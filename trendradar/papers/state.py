"""Paper metadata and judgments in the content database; failures are not hits."""

import hashlib
import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    id TEXT PRIMARY KEY, version INTEGER NOT NULL, content TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS judgments (
    cache_key TEXT PRIMARY KEY, status TEXT NOT NULL,
    result TEXT, attempts INTEGER NOT NULL DEFAULT 1, error TEXT);
"""


def initialize(db):
    # executescript would implicitly commit a caller's active transaction.
    for statement in SCHEMA.split(";"):
        if statement.strip():
            db.execute(statement)


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()


class State:
    def __init__(self, path=None, *, connection=None):
        self.owns_connection = connection is None
        if connection is None:
            from trendradar.content_pool.store import open_database
            self.db = open_database(path)
        else:
            self.db = connection

    def save_papers(self, papers):
        with self.db:
            for paper in papers:
                self.db.execute(
                    """INSERT INTO papers VALUES (?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET version=excluded.version, content=excluded.content
                    WHERE excluded.version >= papers.version""",
                    (
                        paper.canonical_id,
                        paper.version,
                        json.dumps(paper.to_dict(), ensure_ascii=False),
                    ),
                )

    def papers(self):
        from .arxiv import Paper

        return [
            Paper(**json.loads(row[0]))
            for row in self.db.execute("SELECT content FROM papers")
        ]

    def cached(self, key):
        row = self.db.execute(
            "SELECT result FROM judgments WHERE cache_key=? AND status='success'",
            (key,),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def record(self, key, result=None, error=None):
        with self.db:
            self.db.execute(
                """INSERT INTO judgments VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(cache_key) DO UPDATE SET status=excluded.status,
                result=excluded.result, attempts=judgments.attempts+1, error=excluded.error""",
                (key, "failed" if error else "success", json.dumps(result), error),
            )

    def close(self):
        if self.owns_connection:
            self.db.close()
