"""Explicit content processing and daily-preview commands; never sends messages."""

import argparse
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from trendradar.content_pool import Store
from trendradar.content_pool.digest import Digests
from trendradar.content_pool.llm import Meter
from trendradar.content_pool.pipeline import Processor
from trendradar.papers.config import load


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
    args = parser.parse_args()
    from trendradar.content_pool.runtime import load_env

    for filename in ("ai.local.env", "email.local.env"):
        load_env(ROOT / "config" / filename)
    cfg = yaml.safe_load(Path(args.config).read_text())
    path = lambda name: (ROOT / cfg[name]).resolve()
    store = Store(path("db_path"))
    processor = None
    try:
        if args.command in ("migrate", "stats"):
            result = store.stats()
        elif args.command == "correct":
            store.correct(args.platform, args.content_id, args.category)
            result = {"status": "pending_reprocessing"}
        else:
            paper = load(path("papers_config"))
            paper["state_path"] = str((ROOT / paper["state_path"]).resolve())
            raw = yaml.safe_load(path("ai_config").read_text()) or {}
            from trendradar.core.loader import _load_ai_config

            ai = _load_ai_config(raw)
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
