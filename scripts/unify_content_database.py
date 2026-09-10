"""Back up the content pool and import legacy paper candidates/cache; never sends."""

import argparse
import json
import sys
import sqlite3
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from trendradar.content_pool.migration import merge_paper_state
from trendradar.content_pool.runtime import run_lock
from trendradar.content_pool.store import Store
import yaml


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config/content_pool.yaml")
    parser.add_argument("--paper-db", type=Path, default=ROOT / "output/papers/state.sqlite3")
    parser.add_argument("--execute", action="store_true", help="Default is a read-only import plan")
    args = parser.parse_args()
    cfg = yaml.safe_load(args.config.read_text())
    path = (ROOT / cfg["db_path"]).resolve()
    # Match the existing service wrapper plus direct Python entrypoints.
    with run_lock(path.parent / "scheduler.lock"), run_lock(path.with_suffix(".run.lock")):
        store = Store(path) if args.execute else SimpleNamespace(
            path=path, db=sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        )
        try:
            result = merge_paper_state(store, args.paper_db, execute=args.execute)
            print(json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            store.db.close()


if __name__ == "__main__":
    main()
