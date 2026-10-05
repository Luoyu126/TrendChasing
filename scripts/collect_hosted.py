"""Hourly ingestion plus one model call, spaced a full collect interval apart. No SMTP."""
import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.collect_content import collect, load_config, sources as iter_sources
from trendradar.content_pool.llm import BudgetExceeded, Meter
from trendradar.content_pool.pace import (
    COLLECT_WINDOW_SECONDS,
    call_is_due,
    maximum_gap,
    one_call_ids,
)
from trendradar.content_pool.paper_ingest import ingest_papers
from trendradar.content_pool.pipeline import ARXIV, Processor, clean
from trendradar.content_pool.runtime import configuration, run_lock
from trendradar.content_pool.store import Store, now
from trendradar.papers.arxiv import fetch


def papers_collect(store, paper):
    """Return (metadata_failures, fetch_succeeded). A fetched feed counts as success."""
    if not paper['enabled']:
        return 0, False
    with store.db:
        run = store.db.execute("INSERT INTO fetch_runs(source_id,started_at,status) VALUES ('arxiv',?,'running')", (now(),)).lastrowid
    try:
        found = fetch(paper, datetime.now(UTC))
        with store.db:
            ingest_papers(store.db, found)
            store.db.execute("UPDATE fetch_runs SET finished_at=?,status=?,item_count=? WHERE id=?", (now(), 'success' if found else 'empty', len(found), run))
    except Exception as exc:
        with store.db:
            store.db.execute("UPDATE fetch_runs SET finished_at=?,status='failed',error=? WHERE id=?", (now(), type(exc).__name__, run))
        print('arxiv: failed (' + type(exc).__name__ + ')')
        return 1, False
    # Resolve explicit arXiv links outside the daily task; no model classification.
    processor = Processor(store, SimpleNamespace(config={'retries': 1}), paper)
    failures = 0
    for item in store.candidates():
        text, links = clean(item)
        if item['platform'] == 'paper' or not ARXIV.search(text + ' ' + ' '.join(links)):
            continue
        try:
            processor.resolve(item, {})
        except Exception as exc:
            failures += 1
            store.error(item, 'paper_metadata:' + type(exc).__name__)
    if failures:
        with store.db:
            store.db.execute("UPDATE fetch_runs SET status='partial',error=? WHERE id=?", (f'paper_metadata_failures:{failures}', run))
    print(f'arxiv: returned={len(found)}, metadata_failures={failures}')
    return int(bool(failures)), True


def fresh_keys(db, started):
    return {
        (row[0], row[1])
        for row in db.execute(
            "SELECT platform,content_id FROM records WHERE first_seen_at>=?",
            (started,),
        )
    }


def last_model_elapsed(db, clock):
    row = db.execute(
        "SELECT max(created_at) FROM usage_events WHERE event='request_started'"
    ).fetchone()
    if not row or not row[0]:
        return None
    stamp = datetime.fromisoformat(row[0])
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return (clock - stamp).total_seconds()


def classify_once(store, cfg, ai, paper, started, excluded):
    """One model call, and only after the full collect interval since the last one."""
    gap = maximum_gap(1, COLLECT_WINDOW_SECONDS)
    elapsed = last_model_elapsed(store.db, datetime.now(UTC))
    if not call_is_due(elapsed, gap):
        return {"classification": "deferred", "gap_seconds": gap}
    candidates = [
        row for row in store.candidates() if row["platform"] not in excluded
    ]
    selected = one_call_ids(
        candidates, fresh_keys(store.db, started), int(cfg.get("batch_size", 10))
    )
    if not selected:
        return {"classification": "idle", "gap_seconds": gap}
    meter = Meter(
        store, ai, {**cfg, "run_call_budget": 1, "daily_window": None}
    )
    processor = Processor(store, meter, paper)
    try:
        processor.process(candidate_ids=set(selected))
    except BudgetExceeded:
        pass
    finally:
        processor.close()
    return {"classification": "called", "gap_seconds": gap, "calls": meter.calls}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=str(ROOT / 'config/content_pool.yaml'))
    parser.add_argument('--smoke', action='store_true', help='One account per platform, five arXiv candidates, verify idempotent re-ingestion')
    args = parser.parse_args()
    cfg, ai, paper = configuration(ROOT, args.config)
    sources = load_config(cfg['sources_config'], args.config)
    if args.smoke:
        for key in ('accounts', 'zhihu_accounts', 'twitter_accounts'):
            sources[key] = sources[key][:1]
        paper = {**paper, 'max_candidates': 5}
        sources['request_timeout_seconds'] = min(45, sources['request_timeout_seconds'])
    with run_lock(Path(cfg['db_path']).with_suffix('.run.lock')):
        store = Store(cfg['db_path'])
        started = now()
        try:
            paper_failures, paper_ok = papers_collect(store, paper)
            planned = len(list(iter_sources(sources)))
            social_failed = collect(store.db, sources)
            failed = paper_failures + social_failed
            succeeded = int(paper_ok) + planned - social_failed
            if args.smoke:
                from trendradar.content_pool.store import ingest, dumps
                # Replay retained real payloads inside a rolled-back transaction.
                # Re-ingestion must preserve the permanent identity row count.
                count = store.db.execute('SELECT count(*) FROM records').fetchone()[0]
                sample = [dict(r) for r in store.db.execute("SELECT * FROM items WHERE platform IN ('twitter','xiaohongshu','zhihu') ORDER BY last_seen_at DESC LIMIT 20")]
                try:
                    store.db.execute('BEGIN IMMEDIATE')
                    for item in sample:
                        source = store.db.execute('SELECT source_id FROM observations WHERE platform=? AND content_id=? LIMIT 1', (item['platform'],item['content_id'])).fetchone()
                        if source:
                            ingest(store.db, source[0], [item], now())
                    after = store.db.execute('SELECT count(*) FROM records').fetchone()[0]
                    if after != count:
                        raise ValueError('duplicate_ingestion_created_identities')
                finally:
                    store.db.rollback()
                print(dumps({'idempotency': 'passed' if sample else 'no_social_payload_to_test', 'replayed':len(sample), 'records_before':count, 'records_after':after}))
            payload = {'status': 'collected', 'failed_sources': failed, 'succeeded_sources': succeeded}
            if not args.smoke:
                excluded = set()
                if not sources.get('wechat', {}).get('enabled', True):
                    excluded.add('wechat')
                try:
                    payload.update(classify_once(store, cfg, ai, paper, started, excluded))
                except Exception as exc:  # noqa: BLE001 - ingestion already finished; do not leak provider text
                    payload.update({'classification': 'failed', 'category': type(exc).__name__})
            print(json.dumps(payload))
            return 0 if succeeded else 1
        finally:
            store.close()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'category': type(exc).__name__}))
        raise SystemExit(1)
