"""Hourly ingestion only: RSS, arXiv metadata, source outcomes; no AI or SMTP."""
import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.collect_content import collect, load_config
from trendradar.content_pool.paper_ingest import ingest_papers
from trendradar.content_pool.pipeline import ARXIV, Processor, clean
from trendradar.content_pool.runtime import configuration, run_lock
from trendradar.content_pool.store import Store, now
from trendradar.papers.arxiv import fetch


def papers_collect(store, paper):
    if not paper['enabled']:
        return 0
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
        return 1
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
    return int(bool(failures))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=str(ROOT / 'config/content_pool.yaml'))
    args = parser.parse_args()
    cfg, _, paper = configuration(ROOT, args.config)
    sources = load_config(cfg['sources_config'], args.config)
    with run_lock(Path(cfg['db_path']).with_suffix('.run.lock')):
        store = Store(cfg['db_path'])
        try:
            failed = papers_collect(store, paper)
            failed += collect(store.db, sources)
            print(json.dumps({'status': 'collected', 'failed_sources': failed}))
            return int(bool(failed))
        finally:
            store.close()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'category': type(exc).__name__}))
        raise SystemExit(1)
