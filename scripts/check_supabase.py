"""Read-only Supabase connectivity/schema check. Never print credentials or rows."""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from trendradar.content_pool.runtime import load_env


def failure_category(exc):
    message = str(exc).lower()
    safe_codes = {
        "target_schema_already_exists_no_overwrite", "target_snapshot_differs_no_overwrite",
        "sqlite_integrity_check_failed", "sqlite_foreign_key_check_failed",
        "null_primary_key", "missing_database_url", "import_requires_autocommit_connection",
    }
    if isinstance(exc, ValueError) and (
        message in safe_codes or re.fullmatch(r"remote_content_mismatch:[a-z_0-9]+", message)
    ):
        return message
    for pattern, category in (
        ("network is unreachable", "network_unreachable"),
        ("name or service not known", "dns"),
        ("temporary failure in name resolution", "dns"),
        ("password authentication failed", "authentication"),
        ("tenant or user not found", "pooler_address_or_user"),
        ("certificate verify failed", "tls_certificate"),
        ("timeout", "timeout"),
    ):
        if pattern in message:
            return category
    return type(exc).__name__


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", type=Path, default=ROOT / "config/supabase.local.env")
    args = parser.parse_args()
    try:
        load_env(args.env)
        uri = os.environ.get("SUPABASE_DATABASE_URL", "")
        parsed = urlsplit(uri)
        if parsed.scheme not in ("postgres", "postgresql") or not parsed.hostname:
            raise ValueError("missing_database_url")
        import psycopg

        with psycopg.connect(
            uri, connect_timeout=10,
            sslmode="verify-full", sslrootcert=str(ROOT / "config/certs/supabase-ca.crt"),
            options="-c default_transaction_read_only=on -c statement_timeout=10000",
        ) as connection:
            tables = connection.execute(
                """SELECT table_schema,table_name FROM information_schema.tables
                WHERE table_schema IN ('public','trendradar') ORDER BY 1,2"""
            ).fetchall()
            # pg_stat_ssl describes the pooler's upstream connection, not ours.
            tls = bool(connection.pgconn.ssl_in_use)
            print(json.dumps({"status": "connected", "tls": tls,
                              "read_only": True, "application_tables": tables}))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "category": failure_category(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
