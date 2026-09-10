"""Collect, screen, prepare and optionally send the unified content digest."""

import argparse
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.collect_content import collect
from scripts.collect_content import load_config as source_config
from trendradar.content_pool.delivery import deliver
from trendradar.content_pool.digest import Digests
from trendradar.content_pool.llm import Meter
from trendradar.content_pool.pipeline import Processor
from trendradar.content_pool.runtime import configuration, run_lock
from trendradar.content_pool.store import Store, now
from trendradar.papers.arxiv import fetch
from trendradar.content_pool.paper_ingest import ingest_papers


def notice(store, since, paper_failed=False, trial=False):
    failures = {}
    rows = store.db.execute(
        """SELECT source_id,status,started_at FROM fetch_runs WHERE id IN (
        SELECT max(id) FROM fetch_runs GROUP BY source_id)""",
    )
    for row in rows:
        if row["status"] in ("failed", "interrupted", "running", "empty", "partial") or row["started_at"] < since:
            platform = row["source_id"].split(":")[0]
            failures[platform] = failures.get(platform, 0) + 1
    names = {"xiaohongshu": "小红书", "zhihu": "知乎", "twitter": "X", "wechat": "公众号"}
    parts = [f"{names.get(k, k)} {v} 个订阅" for k, v in sorted(failures.items())]
    if paper_failed:
        parts.append("arXiv")
    text = ("采集暂缺：" + "、".join(parts) + "；后续补抓。") if parts else ""
    return ("试运行：近期内容样本。" if trial else "") + text


def collect_papers(processor, trial=False):
    """Persist raw arXiv candidates before any model filtering."""
    cfg = dict(processor.cfg)
    if not cfg["enabled"]:
        return False
    if trial:
        cfg["max_candidates"] = 5
        cfg["request_timeout"] = 20
    try:
        fetched = fetch(cfg, datetime.now(UTC))
        ingest_papers(processor.store.db, fetched)
        return False
    except Exception as exc:  # retained candidates are still processed on failure
        print("arxiv: " + type(exc).__name__, flush=True)
        return True


def run(args):
    if getattr(args, "database_only", False):
        from trendradar.content_pool.hosted import run_daily
        return run_daily(args, ROOT)
    cfg, ai, paper = configuration(ROOT, args.config)
    if args.trial:
        cfg = {
            **cfg,
            "run_call_budget": 20,
            "run_input_budget": 180000,
            "retries": 1,
            "daily_window": None,
        }
    with run_lock(Path(cfg["db_path"]).with_suffix(".run.lock")):
        store = Store(cfg["db_path"])
        processor = None
        try:
            store.db.execute(
                "CREATE TABLE IF NOT EXISTS daily_publications (batch_id TEXT PRIMARY KEY,day TEXT NOT NULL,kind TEXT NOT NULL)"
            )
            store.db.commit()
            from zoneinfo import ZoneInfo

            local_day = (
                datetime.now(ZoneInfo(cfg.get("timezone", "Asia/Shanghai")))
                .date()
                .isoformat()
            )
            if args.send and not args.trial and not args.deliver_batch:  # noqa: SIM102 - keep delivery policy readable
                if store.db.execute(
                    "SELECT 1 FROM daily_publications p JOIN batches b ON b.id=p.batch_id WHERE p.day=? AND p.kind='daily' AND b.status='cleaned'",
                    (local_day,),
                ).fetchone():
                    return {"status": "already_sent", "day": local_day}
            meter = Meter(store, ai, cfg)
            total_calls = meter.config["run_call_budget"]
            total_input = meter.config["run_input_budget"]
            if not args.collect_only:
                meter.config["run_call_budget"] = max(1, total_calls - 5)
                meter.config["run_input_budget"] = max(1, total_input - 60000)
            processor = Processor(store, meter, paper)
            digests = Digests(store, meter, paper, cfg["report_dir"])
            if args.send_ready:
                batch_id = digests.prepare(
                    notice=notice(
                        store,
                        datetime.now(ZoneInfo(cfg["timezone"]))
                        .replace(hour=0, minute=0, second=0, microsecond=0)
                        .astimezone(UTC)
                        .isoformat(),
                    ),
                    allow_empty=True,
                )
                with store.db:
                    store.db.execute(
                        "INSERT OR IGNORE INTO daily_publications VALUES (?,?,?)",
                        (batch_id, local_day, "daily"),
                    )
                digests.preview(batch_id, allow_fallback=True)
                return deliver(digests, batch_id)
            if args.deliver_batch:
                digests.preview(args.deliver_batch, allow_fallback=True)
                return deliver(digests, args.deliver_batch)
            # Resume pending delivery before collecting or spending on new content.
            pending = store.db.execute(
                "SELECT id FROM batches WHERE status='ready' AND EXISTS (SELECT 1 FROM deliveries d WHERE d.batch_id=batches.id) ORDER BY created_at LIMIT 1"
            ).fetchone()
            if pending and args.send:
                return deliver(digests, pending[0])
            from zoneinfo import ZoneInfo

            since = (
                datetime.now(ZoneInfo(cfg.get("timezone", "Asia/Shanghai")))
                .replace(hour=0, minute=0, second=0, microsecond=0)
                .astimezone(UTC)
                .isoformat()
            )
            if args.trial:
                since = now()
            if not args.skip_collect:
                sources = source_config(cfg["sources_config"], pool_config=args.config)
                sources["database"] = Path(cfg["db_path"])
                import os

                sources["rsshub_url"] = os.environ.get(
                    "RSSHUB_URL", sources["rsshub_url"]
                )
                if args.trial:
                    for key in ("accounts", "zhihu_accounts", "twitter_accounts"):
                        sources[key] = sources[key][:2]
                    sources["request_timeout_seconds"] = 25
                collect(store.db, sources)
            paper_failed = False if args.skip_collect else collect_papers(processor, args.trial)
            print("unified candidate screening started", flush=True)
            processor.process(limit=13 if args.trial else None)
            # A first trial may have no newly discovered IDs because historical
            # ingestion already fetched them. Select a bounded recent sample explicitly.
            history = False
            if (
                args.trial
                and not store.db.execute(
                    "SELECT 1 FROM pool_entries WHERE history=0"
                ).fetchone()
            ):
                history = True
                candidates = store.candidates(True)
                candidates.sort(key=lambda r: r["published_at"] or "", reverse=True)
                selected = []
                for platform in ("twitter", "zhihu", "xiaohongshu", "wechat"):
                    selected += [
                        (r["platform"], r["content_id"])
                        for r in candidates
                        if r["platform"] == platform
                    ][:2]
                processor.process(history=True, candidate_ids=set(selected))
            paper_count = store.db.execute(
                "SELECT count(*) FROM records WHERE platform='paper' AND status='accepted' AND history=0"
            ).fetchone()[0]
            print("screening complete", flush=True)
            if args.collect_only:
                return {
                    "status": "collected",
                    "run_id": meter.run_id,
                    "papers_accepted": paper_count,
                }
            meter.config["run_call_budget"] = total_calls
            meter.config["run_input_budget"] = total_input
            message = notice(store, since, paper_failed, args.trial)
            batch_id = digests.prepare(
                history=history, notice=message, allow_empty=True
            )
            with store.db:
                store.db.execute(
                    "INSERT OR IGNORE INTO daily_publications VALUES (?,?,?)",
                    (batch_id, local_day, "trial" if args.trial else "daily"),
                )
            digests.preview(batch_id, allow_fallback=True)
            print(
                json.dumps(
                    {
                        "batch_id": batch_id,
                        "preview": str(digests.report_dir / (batch_id + ".html")),
                        "notice": message,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            if args.send:
                return deliver(digests, batch_id)
            return {"status": "preview", "batch_id": batch_id, "run_id": meter.run_id}
        finally:
            if processor:
                processor.close()
            store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(ROOT / "config/content_pool.yaml"))
    parser.add_argument(
        "--deliver-batch",
        help="Send/reconcile an existing preview without recollecting",
    )
    parser.add_argument(
        "--trial",
        action="store_true",
        help="Bounded live run: two accounts/platform and up to five arXiv candidates",
    )
    parser.add_argument(
        "--send",
        action="store_true",
        help="Send to configured recipients and clean only after acceptance",
    )
    parser.add_argument("--collect-only", action="store_true")
    parser.add_argument(
        "--send-ready",
        action="store_true",
        help="Send the nightly preview without recollecting or scoring",
    )
    parser.add_argument(
        "--skip-collect",
        action="store_true",
        help="Resume retained candidates without fetching social feeds again",
    )
    parser.add_argument("--database-only", action="store_true", help="Read Supabase candidates and cached paper metadata; no RSS/arXiv discovery")
    parser.add_argument("--date", help="Content date YYYY-MM-DD (default previous local calendar day)")
    parser.add_argument("--dry-run", action="store_true", help="Preview only; never send or acknowledge delivery")
    args = parser.parse_args()
    if args.dry_run and (args.send or args.send_ready or args.deliver_batch):
        parser.error("--dry-run cannot send")
    if args.date and not args.database_only:
        parser.error("--date requires --database-only")
    if args.database_only and (args.trial or args.collect_only or args.send_ready or args.deliver_batch):
        parser.error("--database-only supports --date, --dry-run or --send")
    if args.send_ready and not args.send:
        parser.error("--send-ready requires --send")
    if args.deliver_batch and not args.send:
        parser.error("--deliver-batch requires --send")
    if args.send and args.collect_only:
        parser.error("--send and --collect-only are mutually exclusive")
    logging.getLogger("LiteLLM").setLevel(logging.CRITICAL)
    try:
        result = run(args)
    except Exception as exc:  # noqa: BLE001 - do not expose provider errors or credentials
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": type(exc).__name__,
                    "reason": str(exc)
                    if isinstance(exc, (ValueError, RuntimeError))
                    else "see_stage_status",
                },
                ensure_ascii=False,
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return (
        0
        if result["status"]
        in ("sent", "preview", "collected", "already_cleaned", "already_sent")
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
