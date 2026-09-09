import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from trendradar.content_pool.digest import Digests
from trendradar.content_pool.llm import Meter
from trendradar.content_pool.pipeline import Processor, chunks
from trendradar.content_pool.store import SCHEMA, Store, now
from trendradar.papers.arxiv import Paper
from trendradar.papers.config import validate


def item(identity="1", body="A high-level opinion", platform="twitter"):
    return {
        "platform": platform,
        "content_id": identity,
        "url": "https://example.org/" + identity,
        "title": "Title " + identity,
        "author": "Author " + identity,
        "body_html": "",
        "body_text": body,
        "published_at": "2026-09-09T00:00:00+00:00",
        "quality_flags": "[]",
        "raw_entry": "<entry>raw</entry>",
    }


def paper(identity="arxiv:2601.00001"):
    return Paper(
        identity,
        1,
        "Specific research paper",
        "COMPLETE ABSTRACT " + ("evidence " * 100),
        ["Scientist"],
        ["cs.AI"],
        "2020-01-01T00:00:00+00:00",
        "2020-01-01T00:00:00+00:00",
        "https://arxiv.org/abs/2601.00001",
        [],
    )


class Fake:
    def __init__(self):
        self.calls = []
        self.partial = False
        self.fail = False
        self.last_usage = {"input_tokens": 100, "output_tokens": 20}
        self.last_cost = None

    def chat(self, messages, **kwargs):
        self.calls.append(messages)
        if self.fail:
            raise OSError("provider failure")
        prompt = messages[0]["content"]
        value = json.loads(messages[1]["content"])
        if "Assess academic relevance" in prompt:
            return json.dumps(
                {
                    "relevance_score": 0.9,
                    "reason": "Relevant evidence",
                    "research_topics": ["AI"],
                }
            )
        if "Summarize this fragment" in prompt:
            return json.dumps({"evidence": "high-level opinion"})
        if "Group untrusted" in prompt:
            return json.dumps(
                {
                    "topics": [
                        {
                            "title": "AI discussion",
                            "summary": "Several opinions",
                            "ids": [v["id"] for v in value["articles"]],
                        }
                    ]
                }
            )
        rows = [
            {
                "id": v["id"],
                "category": "low_level" if "paper" in v["body"] else "high_level",
                "reason": "main purpose",
                "insight": "Main point",
                "paper_title": "Specific research paper"
                if "paper" in v["body"]
                else "",
            }
            for v in value
        ]
        if self.partial and len(rows) > 1:
            rows = rows[:1]
            self.partial = False
        return json.dumps({"items": rows})


class Workflow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "pool.sqlite3")
        self.fake = Fake()
        self.cfg = validate(
            {
                "enabled": True,
                "research_question": "AI",
                "state_path": str(self.root / "papers.sqlite3"),
                "daily_output_limit": 1,
            }
        )
        self.meter = Meter(self.store, {"MODEL": "fake"}, factory=lambda _: self.fake)
        self.processor = Processor(
            self.store, self.meter, self.cfg, resolver=lambda *_: paper()
        )
        self.digests = Digests(self.store, self.meter, self.cfg, self.root / "reports")

    def tearDown(self):
        self.processor.close()
        self.store.close()
        self.tmp.cleanup()

    def count(self, table):
        return self.store.db.execute("SELECT count(*) FROM " + table).fetchone()[0]

    def ingest(self, *values):
        self.store.ingest_items("account", values)

    def test_routes_shared_full_abstract_and_old_paper(self):
        self.ingest(
            item("1"),
            item("2", "specific paper"),
            item("3", "this paper has results", platform="zhihu"),
        )
        self.processor.process()
        self.assertEqual(self.count("pool_entries"), 2)
        score_calls = [
            c for c in self.fake.calls if "Assess academic" in c[0]["content"]
        ]
        self.assertEqual(len(score_calls), 1)
        self.assertEqual(
            json.loads(score_calls[0][1]["content"])["paper"]["abstract"],
            paper().abstract,
        )
        self.assertEqual(self.count("entry_sources"), 3)
        calls = len(self.fake.calls)
        self.processor.process()
        self.assertEqual(len(self.fake.calls), calls)

    def test_partial_batch_retries_only_missing(self):
        self.fake.partial = True
        self.ingest(item("1"), item("2"), item("3"))
        self.processor.process()
        self.assertEqual(
            [len(json.loads(c[1]["content"])) for c in self.fake.calls], [3, 1, 1]
        )
        self.assertEqual(self.count("pool_entries"), 3)

    def test_identical_body_across_sources_preserves_metadata(self):
        a = item("1")
        b = dict(a, platform="zhihu", author="Different author")
        self.ingest(a, b)
        self.processor.process()
        self.assertEqual(len(self.fake.calls), 1)
        self.assertEqual(len(json.loads(self.fake.calls[0][1]["content"])), 1)
        self.assertEqual(self.count("pool_entries"), 2)

    def test_cache_invalidation_and_metadata(self):
        a = item()
        self.ingest(a)
        self.processor.process()
        count = len(self.fake.calls)
        self.ingest(dict(a, author="Updated"))
        self.processor.process()
        self.assertEqual(len(self.fake.calls), count)
        self.ingest(dict(a, body_text="New opinion"))
        self.processor.process()
        self.assertEqual(len(self.fake.calls), count + 1)
        self.meter.config["prompt_version"] = "v2"
        self.processor.process()
        self.assertEqual(len(self.fake.calls), count + 2)

    def test_budget_resumes(self):
        self.meter.config.update(batch_size=1, run_call_budget=1)
        self.ingest(item("1"), item("2"))
        self.processor.process()
        self.assertEqual(self.count("pool_entries"), 1)
        meter = Meter(self.store, {"MODEL": "fake"}, factory=lambda _: self.fake)
        proc = Processor(self.store, meter, self.cfg)
        try:
            proc.process()
        finally:
            proc.close()
        self.assertEqual(self.count("pool_entries"), 2)

    def test_long_body_no_silent_loss(self):
        text = "".join(f"观点 αβ {i}\n" for i in range(3000)) + "LAST EVIDENCE"
        self.assertEqual("".join(chunks(text, 100)), text)
        self.ingest(item(body=text))
        self.processor.process()
        self.assertEqual(self.count("pool_entries"), 1)
        calls = [
            json.loads(c[1]["content"])
            for c in self.fake.calls
            if "fragment" in c[0]["content"]
        ]
        self.assertIn("LAST EVIDENCE", "".join(calls))

    def test_manual_low_requires_filter(self):
        self.ingest(item())
        self.processor.process()
        self.store.correct("twitter", "1", "low_level")
        with patch.object(self.processor, "screen", return_value=None):
            self.processor.process()
        self.assertEqual(self.count("pool_entries"), 0)

    def test_identity_failure_retained(self):
        self.ingest(item(body="paper"))
        self.processor.resolver = lambda *_: (_ for _ in ()).throw(
            ValueError("ambiguous")
        )
        self.processor.process()
        self.assertEqual(self.count("items"), 1)
        self.assertEqual(self.count("pool_entries"), 0)

    def test_empty_is_excluded(self):
        self.ingest(item(body=""))
        self.processor.process()
        self.assertEqual(self.count("items"), 0)
        self.assertEqual(len(self.fake.calls), 0)

    def test_approved_import_no_llm(self):
        value = {
            "paper": paper().to_dict(),
            "relevance_score": 0.9,
            "reason": "Relevant",
            "research_topics": ["AI"],
            "ranking_score": 0.8,
        }
        self.processor.import_approved({"papers": [value]})
        self.processor.process()
        self.assertEqual(len(self.fake.calls), 0)
        self.assertEqual(self.count("pool_entries"), 1)

    def test_preview_delivery_cleanup_and_permanent_dedup(self):
        original = item()
        self.ingest(original)
        self.processor.process()
        batch = self.digests.prepare()
        self.assertEqual(self.digests.prepare(), batch)
        self.digests.preview(batch)
        calls = len(self.fake.calls)
        self.digests.preview(batch)
        self.assertEqual(len(self.fake.calls), calls)
        self.assertFalse(self.digests.cleanup(batch)["allowed"])
        self.digests.set_targets(batch, {"email": 2, "other": 1})
        self.digests.acknowledge(batch, "email", 0, "success", "receipt1")
        self.digests.acknowledge(batch, "email", 1, "failed")
        with self.assertRaises(ValueError):
            self.digests.cleanup(batch, True)
        self.ingest(item("late"))
        self.processor.process()
        self.digests.acknowledge(batch, "email", 1, "success", "receipt2")
        self.digests.acknowledge(batch, "other", 0, "success", "receipt3")
        self.digests.cleanup(batch, True)
        self.assertEqual(self.count("items"), 1)
        self.assertFalse(list((self.root / "reports").glob("*")))
        self.ingest(original)
        calls = len(self.fake.calls)
        self.processor.process()
        self.assertEqual(len(self.fake.calls), calls)
        self.assertEqual(self.count("items"), 1)
        self.digests.cleanup(batch, True)

    def test_uncertain_must_be_resolved(self):
        self.ingest(item())
        self.processor.process()
        batch = self.digests.prepare()
        self.digests.preview(batch)
        self.digests.set_targets(batch, {"target": 1})
        self.digests.acknowledge(batch, "target", 0, "uncertain")
        with self.assertRaises(ValueError):
            self.digests.acknowledge(batch, "target", 0, "sending")
        self.assertFalse(self.digests.cleanup(batch)["allowed"])

    def test_low_daily_limit_excess_remains(self):
        for identity in ("arxiv:2601.00001", "arxiv:2601.00002"):
            self.processor.import_approved(
                {
                    "papers": [
                        {
                            "paper": paper(identity).to_dict(),
                            "relevance_score": 0.9,
                            "reason": "Relevant",
                            "research_topics": [],
                            "ranking_score": 0.8,
                        }
                    ]
                }
            )
        batch = self.digests.prepare()
        self.assertEqual(len(json.loads(self.digests.get(batch)["snapshot"])), 1)
        self.assertEqual(
            self.store.db.execute(
                "SELECT count(*) FROM pool_entries WHERE batch_id IS NULL"
            ).fetchone()[0],
            1,
        )

    def test_migration_backups_and_history(self):
        path = self.root / "legacy.db"
        db = sqlite3.connect(path)
        db.executescript(SCHEMA)
        value = item()
        db.execute(
            "INSERT INTO items VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(
                value[k]
                for k in (
                    "platform",
                    "content_id",
                    "url",
                    "title",
                    "author",
                    "body_html",
                    "body_text",
                    "published_at",
                )
            )
            + (now(), now(), value["quality_flags"], value["raw_entry"]),
        )
        db.commit()
        db.close()
        migrated = Store(path)
        try:
            self.assertEqual(len(migrated.candidates()), 0)
            self.assertEqual(len(migrated.candidates(True)), 1)
            self.assertTrue(list(self.root.glob("legacy.db.before-v1-*.bak")))
        finally:
            migrated.close()

    def test_usage_actual_and_estimate_separate(self):
        self.ingest(item())
        self.processor.process()
        row = self.store.stats()["usage"][0]
        self.assertEqual(row["input_tokens"], 100)
        self.assertIsNone(row["cost"])
        self.assertGreater(row["estimated_input"], 100)

    def test_technical_retries_bounded(self):
        self.fake.fail = True
        self.ingest(item())
        self.processor.process()
        self.assertLessEqual(len(self.fake.calls), 4)
        self.assertEqual(self.count("items"), 1)

    def test_cleanup_raw_feed_and_cache_owners(self):
        a = item()
        self.ingest(a)
        self.processor.process()
        with self.store.db:
            run = self.store.db.execute(
                "INSERT INTO fetch_runs(source_id,started_at,status,raw_feed) VALUES ('account',?,'success',?)",
                (now(), b"raw"),
            ).lastrowid
            self.store.db.execute(
                "INSERT INTO fetch_run_items VALUES (?,?,?)", (run, "twitter", "1")
            )
        batch = self.digests.prepare()
        self.digests.preview(batch)
        self.digests.set_targets(batch, {"test": 1})
        self.digests.acknowledge(batch, "test", 0, "success", "receipt")
        self.digests.cleanup(batch, True)
        self.assertIsNone(
            self.store.db.execute("SELECT raw_feed FROM fetch_runs").fetchone()[0]
        )
        self.assertEqual(self.count("analysis_cache"), 0)
        self.assertEqual(self.count("records"), 1)

    def test_daily_cap_survives_cleanup(self):
        for identity in ("arxiv:2601.00001", "arxiv:2601.00002"):
            self.processor.import_approved(
                {
                    "papers": [
                        {
                            "paper": paper(identity).to_dict(),
                            "relevance_score": 0.9,
                            "reason": "Relevant",
                            "research_topics": [],
                            "ranking_score": 0.8,
                        }
                    ]
                }
            )
        batch = self.digests.prepare()
        self.digests.preview(batch)
        self.digests.set_targets(batch, {"test": 1})
        self.digests.acknowledge(batch, "test", 0, "success", "receipt")
        self.digests.cleanup(batch, True)
        with self.assertRaisesRegex(ValueError, "no_eligible"):
            self.digests.prepare()
        self.assertEqual(self.count("pool_entries"), 1)

    def test_paper_interest_change_reuses_classification(self):
        self.ingest(item(body="specific paper"))
        self.processor.process()
        count = len(self.fake.calls)
        self.processor.cfg["research_question"] = "Different research question"
        self.processor.process()
        self.assertEqual(len(self.fake.calls), count + 1)
        self.assertIn("Assess academic", self.fake.calls[-1][0]["content"])

    def test_invalid_summary_retains_batch(self):
        self.ingest(item())
        self.processor.process()
        batch = self.digests.prepare()
        with (
            patch.object(
                self.meter,
                "json",
                return_value={
                    "topics": [{"title": "Bad", "summary": "Bad", "ids": ["invented"]}]
                },
            ),
            self.assertRaises(ValueError),
        ):
            self.digests.preview(batch)
        self.assertEqual(self.digests.get(batch)["status"], "prepared")
        self.assertEqual(self.digests.prepare(), batch)
        self.digests.preview(batch)

    def test_full_abstract_over_budget_is_retained(self):
        self.ingest(item(body="specific paper"))
        self.processor.resolver = lambda *_: Paper(
            **{**paper().to_dict(), "abstract": "full " * 10000}
        )
        self.processor.process()
        self.assertEqual(self.count("pool_entries"), 0)
        self.assertEqual(self.count("items"), 1)
        self.assertFalse(
            any("Assess academic" in c[0]["content"] for c in self.fake.calls)
        )

    def test_history_not_mixed_with_live_paper(self):
        self.ingest(item("history", "specific paper"))
        with self.store.db:
            self.store.db.execute("UPDATE records SET history=1")
        self.processor.process(True)
        self.ingest(item("live", "specific paper"))
        self.processor.process()
        self.assertEqual(self.count("pool_entries"), 2)
        batch = self.digests.prepare()
        snapshot = json.loads(self.digests.get(batch)["snapshot"])
        self.assertEqual(
            [s["content_id"] for r in snapshot for s in r["sources"]], ["live"]
        )

    def test_single_and_batch_fixture_parity(self):
        cases = [
            ("opinion", "I think AI progress is accelerating"),
            ("news", "Company announced funding"),
            ("release", "Released a new model and product"),
            ("explanation", "This specific paper demonstrates results"),
            ("no_link", "A paper called Specific research paper introduces a method"),
        ]
        batch = [
            (name, {"title": name, "body": body, "links": []}) for name, body in cases
        ]
        self.processor.classify(batch)
        expected = {name: self.store.cache_get(name)["category"] for name, _ in cases}
        batch_calls = len(self.fake.calls)
        for name, value in batch:
            self.processor.classify([(name + "-single", value)])
        self.assertEqual(
            expected,
            {
                name: self.store.cache_get(name + "-single")["category"]
                for name, _ in cases
            },
        )
        self.assertEqual(batch_calls, 1)
        self.assertEqual(len(self.fake.calls) - batch_calls, 5)

    def test_rejected_relevance_reconsidered_after_interest_change(self):
        self.ingest(item(body="specific paper"))
        with patch.object(self.processor, "screen", return_value=None):
            self.processor.process()
        self.assertEqual(self.count("pool_entries"), 0)
        self.assertEqual(self.count("items"), 1)
        self.processor.cfg["research_question"] = "New relevant interest"
        self.processor.process()
        self.assertEqual(self.count("pool_entries"), 1)

    def test_arxiv_identity_and_full_abstract_from_adapter(self):
        from types import SimpleNamespace

        from trendradar.papers.arxiv import parse_atom

        raw = (Path(__file__).parent / "fixtures/papers/arxiv.xml").read_bytes()
        response = SimpleNamespace(content=raw, raise_for_status=lambda: None)
        with patch(
            "trendradar.content_pool.pipeline.requests.get", return_value=response
        ) as get:
            found = self.processor.resolve(
                item(body="paper https://arxiv.org/abs/2609.00001v2"), {}
            )
        self.assertEqual(found.version, 2)
        self.assertEqual(
            found.abstract, next(p.abstract for p in parse_atom(raw) if p.version == 2)
        )
        self.assertEqual(get.call_args.kwargs["params"], {"id_list": "2609.00001v2"})
        with patch("trendradar.content_pool.pipeline.requests.get") as get:
            self.processor.resolve(
                item(body="paper https://arxiv.org/abs/2609.00001v2"), {}
            )
            get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
