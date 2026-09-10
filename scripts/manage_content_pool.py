"""Explicit content processing and daily-preview commands; never sends messages."""

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from trendradar.content_pool import Store
from trendradar.content_pool.digest import Digests
from trendradar.content_pool.llm import Meter
from trendradar.content_pool.pipeline import Processor


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(ROOT / "config/content_pool.yaml"))
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("migrate", "stats"):
        sub.add_parser(command)
    for command in ("process", "retry", "prepare"):
        sub.add_parser(command).add_argument("--history", action="store_true")
    sub.add_parser("preview").add_argument("batch_id")
    p = sub.add_parser("correct")
    p.add_argument("platform")
    p.add_argument("content_id")
    p.add_argument("category", choices=["high_level", "low_level"])
    sub.add_parser("import-papers").add_argument(
        "path", help="JSON result of existing paper pipeline"
    )
    p = sub.add_parser("cleanup")
    p.add_argument("batch_id")
    p.add_argument("--execute", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("acknowledge", help="Manual reconciliation after checking SMTP evidence; never sends")
    p.add_argument("batch_id")
    p.add_argument("target", help="Exact email:recipient target from deliveries")
    p.add_argument("--part", type=int, default=0)
    p.add_argument("--state", choices=["success", "failed"], required=True)
    p.add_argument("--receipt", required=True, help="Verified queue/Message-ID evidence or definite rejection reason")
    args = parser.parse_args()
    from trendradar.content_pool.runtime import configuration, run_lock

    cfg, ai, paper = configuration(ROOT, args.config)
    path = lambda name: Path(cfg[name])
    with run_lock(path("db_path").with_suffix(".run.lock")):
        store = Store(path("db_path"))
        processor = None
        try:
            if args.command in ("migrate", "stats"):
                result = store.stats()
            elif args.command == "correct":
                store.correct(args.platform, args.content_id, args.category)
                result = {"status": "pending_reprocessing"}
            else:
                meter = Meter(store, ai, cfg)
                digests = Digests(store, meter, paper, path("report_dir"))
                if args.command in ("process", "retry", "import-papers"):
                    processor = Processor(store, meter, paper)
                    if args.command == "import-papers":
                        processor.import_approved(json.loads(Path(args.path).read_text()))
                        result = store.stats()
                    else:
                        result = processor.process(args.history)
                elif args.command == "prepare":
                    result = {"batch_id": digests.prepare(args.history)}
                elif args.command == "preview":
                    result = digests.preview(args.batch_id)
                    result = {
                        "batch_id": result["batch_id"],
                        "report_dir": str(path("report_dir")),
                    }
                elif args.command == "acknowledge":
                    if not args.receipt.strip():
                        parser.error("evidence is required")
                    digests.acknowledge(args.batch_id, args.target, args.part, args.state, args.receipt)
                    result = {"batch_id": args.batch_id, "status": "reconciled"}
                elif args.command == "cleanup":
                    if args.execute and args.dry_run:
                        parser.error("choose --execute or --dry-run")
                    result = digests.cleanup(args.batch_id, args.execute)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            if processor:
                processor.close()
            store.close()


if __name__ == "__main__":
    main()
