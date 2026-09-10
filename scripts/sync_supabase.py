"""Plan, validate or execute the initial import into a private Supabase schema."""

import argparse
import json
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.check_supabase import failure_category
from trendradar.content_pool.runtime import load_env, run_lock
from trendradar.content_pool.supabase_sync import import_snapshot, inspect_snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config/content_pool.yaml")
    parser.add_argument("--env", type=Path, default=ROOT / "config/supabase.local.env")
    parser.add_argument("--snapshot", type=Path, help="Reuse an already reviewed SQLite snapshot")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--validate", action="store_true", help="Import, verify and roll back on the remote database")
    mode.add_argument("--execute", action="store_true", help="Import and commit only after content verification")
    args = parser.parse_args()
    try:
        snapshot = args.snapshot
        if snapshot is None:
            cfg = yaml.safe_load(args.config.read_text())
            source = (ROOT / cfg["db_path"]).resolve()
            directory = ROOT / "output/supabase"
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
            snapshot = directory / ("content-" + stamp + ".sqlite3")
            with run_lock(source.parent / "scheduler.lock"), run_lock(source.with_suffix(".run.lock")):
                snapshot.touch(mode=0o600, exist_ok=False)
                origin = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
                target = sqlite3.connect(snapshot)
                try:
                    origin.backup(target)
                finally:
                    target.close()
                    origin.close()
        manifest = inspect_snapshot(snapshot)
        report = {"status": "planned", "snapshot": str(snapshot.resolve()),
                  "tables": {n: t["rows"] for n, t in manifest["tables"].items()},
                  "total_rows": sum(t["rows"] for t in manifest["tables"].values())}
        if args.validate or args.execute:
            load_env(args.env)
            import psycopg

            with psycopg.connect(
                os.environ["SUPABASE_DATABASE_URL"], connect_timeout=10, autocommit=True,
                sslmode="verify-full", sslrootcert=str(ROOT / "config/certs/supabase-ca.crt"),
            ) as connection:
                report["status"] = import_snapshot(connection, snapshot, manifest, commit=args.execute)
                report["tls"] = bool(connection.pgconn.ssl_in_use)
        else:
            plan_path = snapshot.with_suffix(".plan.json")
            plan_path.touch(mode=0o600, exist_ok=True)
            plan_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
            report["plan"] = str(plan_path)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        # Server exceptions can contain copied content, receipts or credentials.
        print(json.dumps({"status": "failed", "category": failure_category(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
