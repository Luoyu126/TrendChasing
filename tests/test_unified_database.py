import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import yaml

import test_pool_workflow as workflow
from scripts.collect_content import collect, load_config
from scripts.run_content_daily import collect_papers
from trendradar.content_pool.migration import merge_paper_state
from trendradar.content_pool.paper_ingest import ingest_papers
from trendradar.content_pool.store import Store
from trendradar.papers.config import load
from trendradar.papers.pipeline import run
from trendradar.papers.state import State


class UnifiedPipelineTests(unittest.TestCase):
    setUp = workflow.Workflow.setUp
    tearDown = workflow.Workflow.tearDown
    count = workflow.Workflow.count
    ingest = workflow.Workflow.ingest

    def test_processor_uses_pool_connection_and_never_opens_legacy_path(self):
        self.assertIs(self.processor.state.db, self.store.db)
        self.assertFalse(Path(self.cfg["state_path"]).exists())
        self.processor.close()
        self.assertEqual(self.count("papers"), 0)

    def test_werss_social_and_arxiv_share_filter_cache_and_digest(self):
        raw = b"""<rss><channel><item><title>Article about research</title>
        <link>https://mp.weixin.qq.com/s/example-article</link>
        <description>this specific paper has useful results</description>
        <pubDate>Tue, 08 Sep 2026 16:05:00 GMT</pubDate>
        </item></channel></rss>"""
        cfg = {"wechat_feeds": [{"id": "author", "name": "WeChat author",
                "url": "http://localhost:8001/feed/example.xml"}],
               "rsshub_url": "http://localhost:1200", "request_timeout_seconds": 2}
        collect(self.store.db, cfg, lambda *_: raw)
        self.ingest(workflow.item("social", "this specific paper", "twitter"))
        ingest_papers(self.store.db, [workflow.paper()])
        self.assertEqual(self.count("pool_entries"), 0)
        self.assertEqual(self.count("records"), 3)
        self.processor.process()
        self.assertEqual(self.count("pool_entries"), 1)
        self.assertEqual(self.count("entry_sources"), 3)
        scores = [c for c in self.fake.calls if "Assess academic" in c[0]["content"]]
        self.assertEqual(len(scores), 1)
        batch = self.digests.prepare()
        self.digests.preview(batch)
        snapshot = json.loads(self.digests.get(batch)["snapshot"])
        self.assertEqual({s["platform"] for s in snapshot[0]["sources"]},
                         {"wechat", "twitter", "paper"})
        self.digests.set_targets(batch, {"test": 1})
        self.digests.acknowledge(batch, "test", 0, "success", "test-receipt")
        self.digests.cleanup(batch, True)
        ingest_papers(self.store.db, [workflow.paper()])
        collect(self.store.db, cfg, lambda *_: raw)
        before = len(self.fake.calls)
        self.processor.process()
        self.assertEqual(len(self.fake.calls), before)
        self.assertEqual(self.count("items"), 0)
        self.assertEqual(self.count("judgments"), 1)

    def test_arxiv_collection_precedes_filter_and_filtered_candidates_survive(self):
        with patch("scripts.run_content_daily.fetch", return_value=[workflow.paper()]):
            self.assertFalse(collect_papers(self.processor))
        self.assertEqual(len(self.fake.calls), 0)
        self.assertEqual(self.store.candidates()[0]["platform"], "paper")
        with patch.object(self.processor, "screen", return_value=None):
            self.processor.process()
        self.assertEqual(self.count("items"), 1)
        self.assertEqual(self.count("pool_entries"), 0)
        self.assertEqual(self.store.db.execute("SELECT status FROM records").fetchone()[0], "filtered")

    def test_provider_failure_leaves_raw_candidate_for_retry(self):
        ingest_papers(self.store.db, [workflow.paper()])
        self.fake.fail = True
        self.processor.process()
        self.assertEqual(self.count("items"), 1)
        self.assertEqual(self.count("pool_entries"), 0)
        self.fake.fail = False
        with patch("scripts.run_content_daily.fetch", side_effect=OSError):
            self.assertTrue(collect_papers(self.processor))
        self.processor.process()
        self.assertEqual(self.count("pool_entries"), 1)

    def test_older_paper_version_does_not_replace_candidate(self):
        old = workflow.paper()
        newer = type(old)(**{**old.to_dict(), "version": 2, "abstract": "New complete abstract"})
        ingest_papers(self.store.db, [newer, old])
        self.assertEqual(self.store.candidates()[0]["body_text"], newer.abstract)
        self.assertEqual(self.processor.state.papers()[0].version, 2)

    def test_new_version_with_unchanged_abstract_requires_fresh_evaluation(self):
        old = workflow.paper()
        ingest_papers(self.store.db, [old])
        self.processor.process()
        before = len(self.fake.calls)
        newer = type(old)(**{**old.to_dict(), "version": 2})
        ingest_papers(self.store.db, [newer])
        self.assertIsNone(self.store.candidates()[0]["evaluation_key"])
        self.processor.process()
        self.assertEqual(len(self.fake.calls), before + 1)
        payload = json.loads(self.store.db.execute("SELECT payload FROM pool_entries").fetchone()[0])
        self.assertEqual(payload["paper"]["version"], 2)

    def test_paper_already_delivered_via_social_source_never_rescores(self):
        with self.store.db:
            self.store.db.execute("INSERT INTO work_records VALUES (?,'delivered',1)",
                                  (workflow.paper().canonical_id,))
        ingest_papers(self.store.db, [workflow.paper()])
        self.processor.process()
        self.assertEqual(len(self.fake.calls), 0)
        self.assertEqual(self.count("items"), 0)
        self.assertEqual(self.store.db.execute("SELECT status FROM records").fetchone()[0], "duplicate")

    def test_standalone_paper_pipeline_shares_raw_candidates_and_cache(self):
        cfg = {**self.cfg, "state_path": str(self.store.path)}
        result = run(cfg, self.meter.ai, papers=[workflow.paper()], client=self.fake,
                     now=datetime.fromisoformat("2020-01-02T00:00:00+00:00"))
        self.assertEqual(len(result["papers"]), 1)
        self.assertEqual(self.count("items"), 1)
        before = len(self.fake.calls)
        self.processor.process()
        self.assertEqual(len(self.fake.calls), before)
        self.assertEqual(self.count("pool_entries"), 1)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root / "pool.sqlite3")
        self.source = self.root / "legacy.sqlite3"
        state = State(self.source)
        state.save_papers([workflow.paper()])
        state.record("good", result={"relevance_score": .9, "reason": "Relevant", "research_topics": []})
        state.record("failed", error="OSError")
        state.close()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_plan_apply_repeat_keep_receipts_and_original_database(self):
        self.store.ingest_items("account", [workflow.item()])
        with self.store.db:
            self.store.db.execute("INSERT INTO batches VALUES ('b','stamp',0,'cleaned',NULL,NULL,NULL)")
            self.store.db.execute("INSERT INTO deliveries VALUES ('b','recipient',0,'success','receipt','stamp')")
        original = self.source.read_bytes()
        plan = merge_paper_state(self.store, self.source)
        self.assertEqual(plan["status"], "dry_run")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM papers").fetchone()[0], 0)
        result = merge_paper_state(self.store, self.source, execute=True)
        self.assertEqual(result["new_candidates"], 1)
        self.assertEqual(Path(result["backup"]).stat().st_mode & 0o777, 0o600)
        self.assertEqual(merge_paper_state(self.store, self.source, execute=True)["status"], "already_imported")
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(self.store.db.execute("SELECT state,receipt FROM deliveries").fetchone()[:], ("success", "receipt"))
        self.assertEqual(len(self.store.candidates(True)), 1)
        self.assertEqual(len(self.store.candidates()), 1)
        self.assertEqual(self.store.db.execute("SELECT attempts FROM judgments WHERE cache_key='failed'").fetchone()[0], 1)

    def test_import_failure_rolls_back_candidates_cache_and_marker(self):
        def fail(db, papers, **kwargs):
            ingest_papers(db, papers, **kwargs)
            raise RuntimeError("simulated interruption")
        with patch("trendradar.content_pool.migration.ingest_papers", side_effect=fail):
            with self.assertRaises(RuntimeError):
                merge_paper_state(self.store, self.source, execute=True)
        for table in ("papers", "records", "items", "judgments"):
            self.assertEqual(self.store.db.execute("SELECT count(*) FROM " + table).fetchone()[0], 0)
        self.assertEqual(merge_paper_state(self.store, self.source, execute=True)["status"], "imported")

    def test_success_and_delivered_records_are_never_overwritten(self):
        ingest_papers(self.store.db, [workflow.paper()])
        state = State(connection=self.store.db)
        result = {"relevance_score": .8, "reason": "Newer judgment", "research_topics": []}
        state.record("good", result=result)
        state.record("failed", result=result)
        with self.store.db:
            self.store.db.execute("UPDATE records SET status='delivered'")
            self.store.db.execute("DELETE FROM items")
        merge_paper_state(self.store, self.source, execute=True)
        self.assertEqual(state.cached("good"), result)
        self.assertEqual(state.cached("failed"), result)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM items").fetchone()[0], 0)
        self.assertEqual(self.store.db.execute("SELECT status,history FROM records").fetchone()[:], ("delivered", 0))


class DatabaseConfigTests(unittest.TestCase):
    def test_sources_and_papers_resolve_same_authoritative_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pool = root / "content_pool.yaml"
            pool.write_text(yaml.safe_dump({"db_path": str(root / "unified.sqlite3")}))
            source = root / "sources.yaml"
            cfg = {"rsshub_url": "http://localhost:1200", "timezone": "America/New_York",
                   "poll_interval_minutes": 30, "request_timeout_seconds": 20}
            source.write_text(yaml.safe_dump(cfg))
            papers = root / "papers.yaml"
            papers.write_text("enabled: false\n")
            self.assertEqual(load_config(source, pool)["database"], root / "unified.sqlite3")
            self.assertEqual(load(papers)["state_path"], str(root / "unified.sqlite3"))
            cfg["database"] = str(root / "other.sqlite3")
            source.write_text(yaml.safe_dump(cfg))
            with self.assertRaisesRegex(ValueError, "must_match_content_pool"):
                load_config(source, pool)


if __name__ == "__main__":
    unittest.main()
