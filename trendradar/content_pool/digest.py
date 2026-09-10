"""Immutable daily snapshots and explicit delivery/cleanup boundaries. No sender."""

import json
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .store import digest, dumps, now, scrub_responses

SUMMARY = """Group untrusted article insights into at most the supplied max_topics themes.
Write titles and summaries in concise Chinese.
Return JSON {"topics":[{"title":"...","summary":"...","ids":[article IDs]}]}.
Every supplied ID must occur exactly once. Preserve distinct opinions; never invent facts.
Names, platforms and original links are rendered separately from trusted source metadata."""


class Digests:
    def __init__(self, store, meter, paper_config, report_dir):
        self.store, self.meter, self.cfg = store, meter, paper_config
        self.report_dir = Path(report_dir).resolve()

    def prepare(self, history=False, notice="", allow_empty=False):
        db = self.store.db
        window = None
        if self.meter.config.get("daily_window") == "previous_day" and not history:
            from .window import position, previous_day

            window = self.meter.config.get("business_window") or previous_day(now(), self.meter.config["timezone"])
        try:
            db.execute("BEGIN IMMEDIATE")
            report_date = self.meter.config.get("report_date")
            if report_date:
                existing = db.execute("SELECT batch_id FROM digest_days WHERE day=?", (report_date,)).fetchone()
                if existing:
                    db.commit()
                    return existing[0]
            existing = None if report_date else db.execute(
                "SELECT id FROM batches WHERE history=? AND status IN ('prepared','ready') ORDER BY created_at LIMIT 1",
                (int(history),),
            ).fetchone()
            if existing:
                db.commit()
                return existing[0]
            rows = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM pool_entries WHERE history=? AND batch_id IS NULL ORDER BY created_at,id",
                    (int(history),),
                )
            ]
            if window:
                eligible = []
                for row in rows:
                    dates = [
                        r[0]
                        for r in db.execute(
                            "SELECT i.published_at FROM entry_sources s JOIN items i USING(platform,content_id) WHERE s.entry_id=?",
                            (row["id"],),
                        )
                    ]
                    if dates and all(
                        position(d, window) in ("primary", "supplement") for d in dates
                    ):
                        eligible.append(row)
                rows = eligible
            high = [r for r in rows if r["category"] == "high_level"]
            low = [r for r in rows if r["category"] == "low_level"]
            low.sort(
                key=lambda r: (
                    -json.loads(r["payload"]).get("ranking_score", 0),
                    r["id"],
                )
            )
            today = (
                datetime.fromisoformat(now())
                .astimezone(
                    ZoneInfo(self.meter.config.get("timezone", "Asia/Shanghai"))
                )
                .date()
            )
            used = sum(
                r["count"]
                for r in db.execute(
                    """SELECT b.created_at,count(*) AS count FROM batch_papers p
                JOIN batches b ON b.id=p.batch_id WHERE b.history=? GROUP BY b.id""",
                    (int(history),),
                )
                if datetime.fromisoformat(r["created_at"])
                .astimezone(
                    ZoneInfo(self.meter.config.get("timezone", "Asia/Shanghai"))
                )
                .date()
                == today
            )
            if report_date:
                used = 0  # one immutable batch per business date, including backfills
            rows = high + low[: max(0, self.cfg["daily_output_limit"] - used)]
            if not rows and not allow_empty:
                raise ValueError("no_eligible_content")
            batch_id = uuid.uuid4().hex
            stamp = now()
            snapshot = []
            for row in rows:
                sources = [
                    dict(r)
                    for r in db.execute(
                        """SELECT s.platform,s.content_id,s.insight,i.author,i.url,i.title,i.published_at
                    FROM entry_sources s JOIN items i USING(platform,content_id) WHERE entry_id=? ORDER BY s.platform,s.content_id""",
                        (row["id"],),
                    )
                ]
                for source in sources:
                    if window:
                        source["supplement"] = (
                            position(source["published_at"], window) == "supplement"
                        )
                    source["discovered_via"] = [
                        r[0]
                        for r in db.execute(
                            "SELECT source_id FROM observations WHERE platform=? AND content_id=?",
                            (source["platform"], source["content_id"]),
                        )
                    ]
                snapshot.append(
                    {
                        "id": row["id"],
                        "category": row["category"],
                        "payload": json.loads(row["payload"]),
                        "sources": sources,
                    }
                )
                db.execute(
                    "UPDATE pool_entries SET batch_id=? WHERE id=?",
                    (batch_id, row["id"]),
                )
                if row["category"] == "low_level":
                    db.execute(
                        "INSERT INTO batch_papers VALUES (?,?)",
                        (batch_id, json.loads(row["payload"])["paper"]["canonical_id"]),
                    )
            db.execute(
                "INSERT INTO batches VALUES (?,?,?,?,?,NULL,NULL)",
                (batch_id, stamp, int(history), "prepared", dumps(snapshot)),
            )
            if report_date:
                db.execute("INSERT INTO digest_days(day,batch_id) VALUES (?,?)", (report_date, batch_id))
            # Window/notice must commit with the immutable batch, so a crash
            # cannot lose its business date or collection diagnostics.
            for key, stage, value in (("window:" + batch_id, "window", window), ("notice:" + batch_id, "notice", notice)):
                db.execute("INSERT OR IGNORE INTO cache_owners VALUES (?,?)", (key, "batch:" + batch_id))
                db.execute("INSERT INTO analysis_cache VALUES (?,?,?,?) ON CONFLICT(key) DO NOTHING", (key, stage, dumps(value), now()))
            db.commit()
            return batch_id
        except BaseException:
            db.rollback()
            raise

    def get(self, batch_id):
        row = self.store.db.execute(
            "SELECT * FROM batches WHERE id=?", (batch_id,)
        ).fetchone()
        if row is None:
            raise ValueError("unknown_batch")
        return dict(row)

    def summarize(self, high, owner):
        if not high:
            return {"topics": []}
        value = {
            "max_topics": self.meter.config["max_topics"],
            "articles": [
                {"id": r["id"], "insight": r["payload"]["insight"]} for r in high
            ],
        }
        key = digest(
            [
                value,
                self.meter.identity("summary"),
                self.meter.ai.get("API_BASE"),
                self.meter.config["summary_version"],
                SUMMARY,
            ]
        )
        self.store.own_cache(key, owner)
        cached = self.meter.cached("summary", key)
        if cached is not None:
            return cached
        # Large days are summarized in bounded groups, then their compact themes
        # are merged; each original article ID is retained outside model text.
        messages = [
            {"role": "system", "content": SUMMARY},
            {"role": "user", "content": dumps(value)},
        ]
        if self.meter.estimate(messages) > self.meter.config["input_limit"]:
            if len(high) == 1:
                raise ValueError("single_insight_exceeds_summary_budget")
            middle = len(high) // 2
            halves = [
                self.summarize(high[:middle], owner),
                self.summarize(high[middle:], owner),
            ]
            groups = [t for half in halves for t in half["topics"]]
            compact = [
                {
                    "id": "group-" + str(i),
                    "category": "high_level",
                    "payload": {"insight": t["title"] + ": " + t["summary"]},
                }
                for i, t in enumerate(groups)
            ]
            # Bound recursion even if the model refuses to compress.
            if len(dumps(compact).encode()) >= len(dumps(high).encode()):
                raise ValueError("summary_did_not_reduce_input")
            merged = self.summarize(compact, owner)
            result = {
                "topics": [
                    dict(
                        t,
                        ids=[
                            article
                            for gid in t["ids"]
                            for article in groups[int(gid.split("-")[1])]["ids"]
                        ],
                    )
                    for t in merged["topics"]
                ]
            }
        else:
            result = self.meter.json("summary", SUMMARY, value)
        topics = result.get("topics") if isinstance(result, dict) else None
        if (
            not isinstance(topics, list)
            or not 1 <= len(topics) <= self.meter.config["max_topics"]
        ):
            raise ValueError("invalid_topics")
        if any(
            not isinstance(t, dict)
            or not isinstance(t.get("ids"), list)
            or not all(
                isinstance(t.get(k), str) and t[k].strip() for k in ("title", "summary")
            )
            for t in topics
        ):
            raise ValueError("invalid_topic_shape")
        ids = [i for t in topics for i in t["ids"]]
        if sorted(ids) != sorted(r["id"] for r in high):
            raise ValueError("topic_membership_mismatch")
        self.store.cache_put(key, "summary", result)
        return result

    def preview(self, batch_id, allow_fallback=False):
        batch = self.get(batch_id)
        if batch["status"] == "cleaned":
            raise ValueError("batch_payload_cleaned")
        snapshot = json.loads(batch["snapshot"])
        try:
            summary = (
                json.loads(batch["summary"])
                if batch["summary"]
                else self.summarize(
                    [r for r in snapshot if r["category"] == "high_level"],
                    "batch:" + batch_id,
                )
            )
        except Exception:
            if not allow_fallback:
                raise
            ids = [r["id"] for r in snapshot if r["category"] == "high_level"]
            summary = {
                "topics": [
                    {
                        "title": "本期观点",
                        "summary": "以下为已完成整理的观点与时事。",
                        "ids": ids,
                    }
                ]
                if ids
                else [],
                "fallback": True,
            }
        with self.store.db:
            self.store.db.execute(
                "UPDATE batches SET summary=?,status='ready',error=NULL WHERE id=?",
                (dumps(summary), batch_id),
            )
        result = {
            "batch_id": batch_id,
            "created_at": batch["created_at"],
            "high_level": summary["topics"],
            "entries": snapshot,
            "window": self.store.cache_get("window:" + batch_id),
            "notice": self.store.cache_get("notice:" + batch_id) or "",
        }
        if summary.get("fallback"):
            result["notice"] += "主题归纳暂缺，已保留逐篇观点。"
        self.report_dir.mkdir(parents=True, exist_ok=True)
        from .presentation import render

        rendered = render(result, self.meter.config.get("timezone", "Asia/Shanghai"))
        for suffix, body in (
            ("json", json.dumps(result, ensure_ascii=False, indent=2)),
            ("html", rendered),
        ):
            path = self.report_dir / (batch_id + "." + suffix)
            # Register before writing so a crash never leaves an untracked report.
            with self.store.db:
                self.store.db.execute(
                    "INSERT OR IGNORE INTO artifacts VALUES (?,?)",
                    (batch_id, str(path)),
                )
            path.write_text(body, encoding="utf-8")
            path.chmod(0o600)
        return result

    def set_targets(self, batch_id, targets):
        """targets maps each destination to its expected number of message parts."""
        if not targets or any(
            not isinstance(k, str) or not k or type(v) is not int or v < 1
            for k, v in targets.items()
        ):
            raise ValueError("nonempty_targets_required")
        with self.store.db:
            batch = self.get(batch_id)
            if batch["status"] != "ready":
                raise ValueError("preview_required")
            old = list(
                self.store.db.execute(
                    "SELECT state FROM deliveries WHERE batch_id=?", (batch_id,)
                )
            )
            if old:
                raise ValueError("targets_already_fixed")
            for target, count in targets.items():
                for part in range(count):
                    self.store.db.execute(
                        "INSERT INTO deliveries VALUES (?,?,?,?,NULL,?)",
                        (batch_id, target, part, "pending", now()),
                    )

    def acknowledge(self, batch_id, target, part, state, receipt=None):
        transitions = {
            "pending": {"sending", "success", "failed", "uncertain"},
            "sending": {"success", "failed", "uncertain"},
            "failed": {"sending", "success", "uncertain"},
            "uncertain": {"success", "failed"},
            "success": {"success"},
        }
        with self.store.db:
            row = self.store.db.execute(
                "SELECT state FROM deliveries WHERE batch_id=? AND target=? AND part=?",
                (batch_id, target, part),
            ).fetchone()
            if row is None or state not in transitions[row[0]]:
                raise ValueError("invalid_delivery_transition")
            if state == "success" and not receipt:
                raise ValueError("success_requires_receipt")
            if row[0] == "success":
                return
            self.store.db.execute(
                "UPDATE deliveries SET state=?,receipt=?,updated_at=? WHERE batch_id=? AND target=? AND part=?",
                (state, receipt, now(), batch_id, target, part),
            )

    def cleanup(self, batch_id, execute=False):
        db = self.store.db
        try:
            db.execute("BEGIN IMMEDIATE")
            self.get(batch_id)
            states = [
                r[0]
                for r in db.execute(
                    "SELECT state FROM deliveries WHERE batch_id=?", (batch_id,)
                )
            ]
            allowed = bool(states) and all(s == "success" for s in states)
            entries = [
                r[0]
                for r in db.execute(
                    "SELECT id FROM pool_entries WHERE batch_id=?", (batch_id,)
                )
            ]
            paths = [
                r[0]
                for r in db.execute(
                    "SELECT path FROM artifacts WHERE batch_id=?", (batch_id,)
                )
            ]
            result = {
                "batch_id": batch_id,
                "allowed": allowed,
                "entries": len(entries),
                "files": paths,
                "executed": False,
            }
            if not execute:
                db.rollback()
                return result
            if not allowed:
                raise ValueError("all_delivery_parts_must_succeed_before_cleanup")
            for entry_id in entries:
                row = db.execute(
                    "SELECT category,payload FROM pool_entries WHERE id=?", (entry_id,)
                ).fetchone()
                if row["category"] == "low_level":
                    db.execute(
                        "UPDATE work_records SET status='delivered' WHERE id=?",
                        (json.loads(row["payload"])["paper"]["canonical_id"],),
                    )
                sources = list(
                    db.execute(
                        "SELECT platform,content_id FROM entry_sources WHERE entry_id=?",
                        (entry_id,),
                    )
                )
                db.execute("DELETE FROM pool_entries WHERE id=?", (entry_id,))
                for platform, content_id in sources:
                    if db.execute(
                        "SELECT 1 FROM entry_sources WHERE platform=? AND content_id=?",
                        (platform, content_id),
                    ).fetchone():
                        continue
                    db.execute(
                        "UPDATE records SET status='delivered',reason=NULL WHERE platform=? AND content_id=?",
                        (platform, content_id),
                    )
                    db.execute(
                        "DELETE FROM items WHERE platform=? AND content_id=?",
                        (platform, content_id),
                    )
                    self.store.release_caches([dumps([platform, content_id])])
            self.store.release_caches(["batch:" + batch_id])
            scrub_responses(db)
            db.execute(
                "UPDATE batches SET status='cleaned',snapshot=NULL,summary=NULL WHERE id=?",
                (batch_id,),
            )
            db.commit()
        except BaseException:
            db.rollback()
            raise
        for value in paths:
            path = Path(value)
            # Other runners may have different checkout directories. Their
            # disposable files must never be accessed on this machine.
            if path.parent.resolve() == self.report_dir and path.name in (
                batch_id + ".json", batch_id + ".html",
            ):
                path.unlink(missing_ok=True)
            with db:
                db.execute(
                    "DELETE FROM artifacts WHERE batch_id=? AND path=?",
                    (batch_id, value),
                )
        result["executed"] = True
        return result
