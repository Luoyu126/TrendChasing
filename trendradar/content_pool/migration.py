"""Copy legacy paper state into the existing pool without deleting either source."""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from trendradar.papers.arxiv import Paper
from trendradar.papers.pipeline import validate_result

from .paper_ingest import ingest_papers
from .store import digest


def merge_paper_state(store, source, *, execute=False):
    source = Path(source).resolve()
    if source == store.path.resolve():
        raise ValueError("legacy_source_must_differ_from_pool")
    if not source.is_file():
        raise ValueError("legacy_paper_database_missing")
    legacy = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    try:
        legacy.execute("BEGIN")
        papers = legacy.execute("SELECT id,version,content FROM papers ORDER BY id").fetchall()
        judgments = legacy.execute(
            "SELECT cache_key,status,result,attempts,error FROM judgments ORDER BY cache_key"
        ).fetchall()
    finally:
        legacy.close()
    # Validate before touching the destination; never log content or model errors.
    parsed = []
    for identity, version, content in papers:
        paper = Paper(**json.loads(content))
        if paper.canonical_id != identity or paper.version != version or not paper.abstract.strip():
            raise ValueError("invalid_legacy_paper")
        parsed.append(paper)
    for _, status, result, attempts, _ in judgments:
        if status not in ("success", "failed") or attempts < 1:
            raise ValueError("invalid_legacy_judgment")
        if status == "success":
            validate_result(json.loads(result))
    fingerprint = digest([papers, judgments])
    report = {"status": "dry_run", "source_papers": len(papers),
              "source_judgments": len(judgments), "source_preserved": True}
    exists = store.db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='state_imports'"
    ).fetchone()
    if exists and store.db.execute(
        "SELECT 1 FROM state_imports WHERE fingerprint=?", (fingerprint,)
    ).fetchone():
        return {**report, "status": "already_imported"}
    if not execute:
        return report
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    backup = store.path.with_name(store.path.name + ".before-unify-" + stamp + ".bak")
    # Create with restrictive permissions before SQLite writes any private data.
    backup.touch(mode=0o600, exist_ok=False)
    target = sqlite3.connect(backup)
    try:
        store.db.backup(target)
    finally:
        target.close()
    with store.db:
        store.db.execute("BEGIN IMMEDIATE")
        store.db.execute("""CREATE TABLE IF NOT EXISTS state_imports (
            fingerprint TEXT PRIMARY KEY,imported_at TEXT NOT NULL)""")
        if store.db.execute(
            "SELECT 1 FROM state_imports WHERE fingerprint=?", (fingerprint,)
        ).fetchone():
            return {**report, "status": "already_imported"}
        # Newly discovered legacy rows remain historical, existing receipts and
        # history flags are retained. Nothing is implicitly approved or sent.
        added = ingest_papers(store.db, parsed, source_id="paper_legacy", history=True)
        for key, status, result, attempts, error in judgments:
            store.db.execute(
                """INSERT INTO judgments VALUES (?,?,?,?,?)
                ON CONFLICT(cache_key) DO UPDATE SET
                status=excluded.status,result=excluded.result,
                attempts=max(judgments.attempts,excluded.attempts),error=excluded.error
                WHERE judgments.status!='success' AND excluded.status='success'""",
                (key, status, result, attempts, error),
            )
        store.db.execute("INSERT INTO state_imports VALUES (?,?)", (fingerprint, stamp))
    return {**report, "status": "imported", "new_candidates": added, "backup": str(backup)}
