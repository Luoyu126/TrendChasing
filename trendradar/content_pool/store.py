"""Local persistence. Payloads are disposable; identities and receipts are not."""

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path


def now():
    return datetime.now(UTC).isoformat()


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
 platform TEXT NOT NULL,content_id TEXT NOT NULL,url TEXT NOT NULL,title TEXT NOT NULL,
 author TEXT NOT NULL,body_html TEXT NOT NULL,body_text TEXT NOT NULL,published_at TEXT,
 first_seen_at TEXT NOT NULL,last_seen_at TEXT NOT NULL,quality_flags TEXT NOT NULL,
 raw_entry TEXT NOT NULL,PRIMARY KEY(platform,content_id));
CREATE INDEX IF NOT EXISTS items_date ON items(published_at);
CREATE TABLE IF NOT EXISTS records (
 platform TEXT NOT NULL,content_id TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',
 first_seen_at TEXT NOT NULL,last_seen_at TEXT NOT NULL,history INTEGER NOT NULL DEFAULT 0,
 body_hash TEXT NOT NULL,reason TEXT,override TEXT,evaluation_key TEXT,
 PRIMARY KEY(platform,content_id));
CREATE TABLE IF NOT EXISTS observations (
 source_id TEXT NOT NULL,platform TEXT NOT NULL,content_id TEXT NOT NULL,
 first_seen_at TEXT NOT NULL,last_seen_at TEXT NOT NULL,
 PRIMARY KEY(source_id,platform,content_id),
 FOREIGN KEY(platform,content_id) REFERENCES records(platform,content_id));
CREATE TABLE IF NOT EXISTS fetch_runs (
 id INTEGER PRIMARY KEY,source_id TEXT NOT NULL,started_at TEXT NOT NULL,finished_at TEXT,
 status TEXT NOT NULL,item_count INTEGER NOT NULL DEFAULT 0,
 flagged_count INTEGER NOT NULL DEFAULT 0,error TEXT,raw_feed BLOB);
CREATE TABLE IF NOT EXISTS fetch_run_items (
 run_id INTEGER NOT NULL,platform TEXT NOT NULL,content_id TEXT NOT NULL,
 PRIMARY KEY(run_id,platform,content_id),FOREIGN KEY(run_id) REFERENCES fetch_runs(id));
CREATE TABLE IF NOT EXISTS pool_entries (
 id TEXT PRIMARY KEY,category TEXT NOT NULL CHECK(category IN ('high_level','low_level')),
 payload TEXT NOT NULL,history INTEGER NOT NULL,created_at TEXT NOT NULL,batch_id TEXT);
CREATE TABLE IF NOT EXISTS entry_sources (
 entry_id TEXT NOT NULL,platform TEXT NOT NULL,content_id TEXT NOT NULL,
 insight TEXT NOT NULL,PRIMARY KEY(entry_id,platform,content_id),
 FOREIGN KEY(entry_id) REFERENCES pool_entries(id) ON DELETE CASCADE,
 FOREIGN KEY(platform,content_id) REFERENCES records(platform,content_id));
CREATE TABLE IF NOT EXISTS work_records (id TEXT PRIMARY KEY,status TEXT NOT NULL,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS analysis_cache (
 key TEXT PRIMARY KEY,stage TEXT NOT NULL,result TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS usage_events (
 id INTEGER PRIMARY KEY,run_id TEXT NOT NULL,stage TEXT NOT NULL,model TEXT,
 event TEXT NOT NULL,attempt INTEGER NOT NULL DEFAULT 0,estimated_input INTEGER NOT NULL DEFAULT 0,
 input_tokens INTEGER,output_tokens INTEGER,cost REAL,error TEXT,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS batches (
 id TEXT PRIMARY KEY,created_at TEXT NOT NULL,history INTEGER NOT NULL,status TEXT NOT NULL,
 snapshot TEXT,summary TEXT,error TEXT);
CREATE TABLE IF NOT EXISTS deliveries (
 batch_id TEXT NOT NULL,target TEXT NOT NULL,part INTEGER NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('pending','sending','success','failed','uncertain')),
 receipt TEXT,updated_at TEXT NOT NULL,PRIMARY KEY(batch_id,target,part),
 FOREIGN KEY(batch_id) REFERENCES batches(id));
CREATE TABLE IF NOT EXISTS batch_papers (
 batch_id TEXT NOT NULL,paper_id TEXT NOT NULL,PRIMARY KEY(batch_id,paper_id));
CREATE TABLE IF NOT EXISTS cache_owners (
 key TEXT NOT NULL,owner TEXT NOT NULL,PRIMARY KEY(key,owner));
CREATE TABLE IF NOT EXISTS artifacts (
 batch_id TEXT NOT NULL,path TEXT NOT NULL,PRIMARY KEY(batch_id,path),
 FOREIGN KEY(batch_id) REFERENCES batches(id));
"""


def open_database(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version > 1:
        db.close()
        raise ValueError("Content pool schema is newer than this application")
    if version == 0:
        legacy = bool(
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='items'"
            ).fetchone()
        )
        if legacy:
            backup = path.with_name(
                path.name
                + ".before-v1-"
                + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
                + ".bak"
            )
            target = sqlite3.connect(backup)
            try:
                db.backup(target)
            finally:
                target.close()
            backup.chmod(0o600)
        try:
            # executescript begins the transaction itself. No partial migration.
            db.executescript("BEGIN IMMEDIATE;" + SCHEMA)
            if legacy:
                for item in db.execute("SELECT * FROM items").fetchall():
                    db.execute(
                        "INSERT OR IGNORE INTO records(platform,content_id,first_seen_at,last_seen_at,history,body_hash) VALUES (?,?,?,?,1,?)",
                        (
                            item["platform"],
                            item["content_id"],
                            item["first_seen_at"],
                            item["last_seen_at"],
                            payload_hash(item),
                        ),
                    )
                db.execute("ALTER TABLE observations RENAME TO legacy_observations")
                db.execute("""CREATE TABLE observations (
                    source_id TEXT NOT NULL,platform TEXT NOT NULL,content_id TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,last_seen_at TEXT NOT NULL,
                    PRIMARY KEY(source_id,platform,content_id),
                    FOREIGN KEY(platform,content_id) REFERENCES records(platform,content_id))""")
                db.execute("INSERT INTO observations SELECT * FROM legacy_observations")
                db.execute("DROP TABLE legacy_observations")
                # Conservatively retain legacy responses until their source's
                # retained payloads are all processed.
                db.execute("""INSERT OR IGNORE INTO fetch_run_items
                    SELECT f.id,o.platform,o.content_id FROM fetch_runs f
                    JOIN observations o ON o.source_id=f.source_id WHERE f.raw_feed IS NOT NULL""")
            db.execute("PRAGMA user_version=1")
            db.commit()
        except BaseException:
            db.rollback()
            db.close()
            raise
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    path.chmod(0o600)
    return db


def payload_hash(item):
    return digest([item["title"], item["body_text"], item["body_html"]])


def ingest(db, source_id, items, stamp):
    """Receive raw candidates, never an implicit admission into the official pool."""
    for item in items:
        key = (item["platform"], item["content_id"])
        record = db.execute(
            "SELECT * FROM records WHERE platform=? AND content_id=?", key
        ).fetchone()
        content_hash = payload_hash(item)
        db.execute(
            """INSERT INTO records(platform,content_id,first_seen_at,last_seen_at,body_hash)
            VALUES (?,?,?,?,?) ON CONFLICT(platform,content_id) DO UPDATE SET last_seen_at=excluded.last_seen_at""",
            (*key, stamp, stamp, content_hash),
        )
        db.execute(
            """INSERT INTO observations VALUES (?,?,?,?,?)
            ON CONFLICT(source_id,platform,content_id) DO UPDATE SET last_seen_at=excluded.last_seen_at""",
            (source_id, *key, stamp, stamp),
        )
        if record and record["status"] in ("delivered", "rejected", "duplicate"):
            continue
        frozen = db.execute(
            """SELECT 1 FROM entry_sources s JOIN pool_entries p ON p.id=s.entry_id
            WHERE s.platform=? AND s.content_id=? AND p.batch_id IS NOT NULL""",
            key,
        ).fetchone()
        old = db.execute(
            "SELECT quality_flags FROM items WHERE platform=? AND content_id=?", key
        ).fetchone()
        if frozen or (
            old
            and not set(json.loads(item["quality_flags"])).issubset(json.loads(old[0]))
        ):
            db.execute(
                "UPDATE items SET last_seen_at=? WHERE platform=? AND content_id=?",
                (stamp, *key),
            )
            continue
        if record and record["body_hash"] != content_hash:
            detach(db, *key)
            db.execute(
                "UPDATE records SET status='pending',evaluation_key=NULL,body_hash=? WHERE platform=? AND content_id=?",
                (content_hash, *key),
            )
        db.execute(
            """INSERT INTO items VALUES (:platform,:content_id,:url,:title,:author,:body_html,:body_text,
            :published_at,:first_seen_at,:last_seen_at,:quality_flags,:raw_entry)
            ON CONFLICT(platform,content_id) DO UPDATE SET url=excluded.url,title=excluded.title,
            author=excluded.author,body_html=excluded.body_html,body_text=excluded.body_text,
            published_at=excluded.published_at,last_seen_at=excluded.last_seen_at,
            quality_flags=excluded.quality_flags,raw_entry=excluded.raw_entry""",
            dict(item, first_seen_at=stamp, last_seen_at=stamp),
        )


def detach(db, platform, content_id):
    db.execute(
        "DELETE FROM entry_sources WHERE platform=? AND content_id=?",
        (platform, content_id),
    )
    db.execute(
        "DELETE FROM pool_entries WHERE batch_id IS NULL AND id NOT IN (SELECT entry_id FROM entry_sources)"
    )


def scrub_responses(db):
    db.execute("""UPDATE fetch_runs SET raw_feed=NULL WHERE raw_feed IS NOT NULL AND status IN ('success','partial','empty') AND NOT EXISTS (
        SELECT 1 FROM fetch_run_items f JOIN items i ON i.platform=f.platform AND i.content_id=f.content_id
        WHERE f.run_id=fetch_runs.id)""")


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.db = open_database(path)

    def close(self):
        self.db.close()

    def ingest_items(self, source_id, items):
        with self.db:
            ingest(self.db, source_id, items, now())

    def own_cache(self, key, owner):
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO cache_owners VALUES (?,?)", (key, owner)
            )

    def release_caches(self, owners):
        keys = set()
        for owner in owners:
            keys.update(
                r[0]
                for r in self.db.execute(
                    "SELECT key FROM cache_owners WHERE owner=?", (owner,)
                )
            )
            self.db.execute("DELETE FROM cache_owners WHERE owner=?", (owner,))
        for key in keys:
            if not self.db.execute(
                "SELECT 1 FROM cache_owners WHERE key=?", (key,)
            ).fetchone():
                self.db.execute("DELETE FROM analysis_cache WHERE key=?", (key,))

    def cache_get(self, key):
        row = self.db.execute(
            "SELECT result FROM analysis_cache WHERE key=?", (key,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def cache_put(self, key, stage, result):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO analysis_cache VALUES (?,?,?,?)",
                (key, stage, dumps(result), now()),
            )

    def candidates(self, history=False):
        return [
            dict(r)
            for r in self.db.execute(
                """SELECT i.*,r.history,r.override,r.evaluation_key
            FROM items i JOIN records r USING(platform,content_id)
            WHERE r.history=? AND r.status IN ('pending','accepted','filtered') AND NOT EXISTS (
              SELECT 1 FROM entry_sources s JOIN pool_entries p ON p.id=s.entry_id
              WHERE s.platform=i.platform AND s.content_id=i.content_id AND p.batch_id IS NOT NULL)
            ORDER BY r.first_seen_at,i.platform,i.content_id""",
                (int(history),),
            )
        ]

    def reject(self, item, reason, status="rejected"):
        with self.db:
            detach(self.db, item["platform"], item["content_id"])
            self.db.execute(
                "UPDATE records SET status=?,reason=? WHERE platform=? AND content_id=?",
                (status, reason, item["platform"], item["content_id"]),
            )
            self.db.execute(
                "DELETE FROM items WHERE platform=? AND content_id=?",
                (item["platform"], item["content_id"]),
            )
            self.release_caches([dumps([item["platform"], item["content_id"]])])
            scrub_responses(self.db)

    def filtered(self, item, reason, evaluation_key):
        # Relevance can change with research interests; preserve these candidates.
        with self.db:
            detach(self.db, item["platform"], item["content_id"])
            self.db.execute(
                "UPDATE records SET status='filtered',reason=?,evaluation_key=? WHERE platform=? AND content_id=?",
                (reason, evaluation_key, item["platform"], item["content_id"]),
            )

    def error(self, item, reason):
        with self.db:
            self.db.execute(
                "UPDATE records SET reason=? WHERE platform=? AND content_id=?",
                (reason, item["platform"], item["content_id"]),
            )

    def admit(self, item, category, payload, insight, evaluation_key, paper_id=None):
        entry_id = (
            "paper:" + paper_id + (":history" if item["history"] else "")
            if paper_id
            else digest([item["platform"], item["content_id"]])
        )
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            record = self.db.execute(
                "SELECT status FROM records WHERE platform=? AND content_id=?",
                (item["platform"], item["content_id"]),
            ).fetchone()
            if record is None or record[0] in ("delivered", "rejected", "duplicate"):
                return False
            current = self.db.execute(
                "SELECT * FROM pool_entries WHERE id=?", (entry_id,)
            ).fetchone()
            if current and current["batch_id"]:
                self.error(item, "waiting_for_frozen_paper_batch")
                return False
            if paper_id:
                work = self.db.execute(
                    "SELECT status FROM work_records WHERE id=?", (paper_id,)
                ).fetchone()
                if work and work[0] == "delivered":
                    self.reject(item, "paper_already_delivered", "duplicate")
                    return False
            if (
                current
                and paper_id
                and json.loads(current["payload"])["paper"]["version"]
                > payload["paper"]["version"]
            ):
                payload = json.loads(current["payload"])
            self.db.execute(
                """INSERT INTO pool_entries VALUES (?,?,?,?,?,NULL)
                ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,history=min(pool_entries.history,excluded.history)""",
                (entry_id, category, dumps(payload), item["history"], now()),
            )
            self.db.execute(
                "INSERT OR REPLACE INTO entry_sources VALUES (?,?,?,?)",
                (entry_id, item["platform"], item["content_id"], insight),
            )
            self.db.execute(
                "UPDATE records SET status='accepted',reason=NULL,evaluation_key=? WHERE platform=? AND content_id=?",
                (evaluation_key, item["platform"], item["content_id"]),
            )
            if paper_id:
                self.db.execute(
                    "INSERT INTO work_records VALUES (?,'accepted',?) ON CONFLICT(id) DO UPDATE SET version=max(version,excluded.version)",
                    (paper_id, payload["paper"]["version"]),
                )
        return True

    def correct(self, platform, content_id, category):
        if category not in ("high_level", "low_level"):
            raise ValueError("Invalid category")
        key = (platform, content_id)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if not self.db.execute(
                "SELECT 1 FROM items WHERE platform=? AND content_id=?", key
            ).fetchone():
                raise ValueError("Payload no longer retained")
            if self.db.execute(
                """SELECT 1 FROM entry_sources s JOIN pool_entries p ON p.id=s.entry_id
                WHERE s.platform=? AND s.content_id=? AND p.batch_id IS NOT NULL""",
                key,
            ).fetchone():
                raise ValueError("Cannot modify a frozen batch")
            detach(self.db, *key)
            self.db.execute(
                "UPDATE records SET override=?,status='pending',evaluation_key=NULL WHERE platform=? AND content_id=?",
                (category, *key),
            )

    def stats(self):
        return {
            "records": [
                dict(r)
                for r in self.db.execute(
                    "SELECT history,status,count(*) AS count FROM records GROUP BY history,status"
                )
            ],
            "pool": [
                dict(r)
                for r in self.db.execute(
                    "SELECT history,category,count(*) AS count FROM pool_entries GROUP BY history,category"
                )
            ],
            "batches": [
                dict(r)
                for r in self.db.execute(
                    "SELECT id,status,created_at FROM batches ORDER BY created_at DESC"
                )
            ],
            "usage": [
                dict(r)
                for r in self.db.execute("""SELECT run_id,stage,model,event,count(*) AS count,
                sum(CASE WHEN attempt>0 THEN 1 ELSE 0 END) AS retries,
                sum(estimated_input) AS estimated_input,sum(input_tokens) AS input_tokens,
                sum(output_tokens) AS output_tokens,sum(cost) AS cost FROM usage_events GROUP BY run_id,stage,model,event""")
            ],
        }
