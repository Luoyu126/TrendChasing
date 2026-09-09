import json
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from trendradar.papers.arxiv import fetch, parse_atom
from trendradar.papers.config import validate
from trendradar.papers.pipeline import (
    cache_key,
    daily_recommendations,
    prefilter,
    run,
    validate_result,
)
from trendradar.papers.render import append_section
from trendradar.papers.state import State

FIXTURES = Path(__file__).parent / "fixtures" / "papers"
NOW = datetime(2026, 9, 9, 12, tzinfo=UTC)


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cfg = validate(
            {
                "research_question": "A complete question about evidence attribution",
                "topics": ["scientific question answering"],
                "state_path": str(Path(self.temp.name) / "state.db"),
            }
        )
        self.papers = parse_atom((FIXTURES / "arxiv.xml").read_bytes())
        self.paper = self.papers[1]
        self.ai = {"MODEL": "test/model", "NUM_RETRIES": 2}
        self.client = Mock()
        self.client.chat.return_value = json.dumps(self.judgment())

    def tearDown(self):
        self.temp.cleanup()

    def judgment(self, score=0.8):
        return {
            "relevance_score": score,
            "reason": "Addresses evidence attribution",
            "research_topics": ["attribution"],
        }

    def process(self, papers=None, **kwargs):
        return run(
            self.cfg,
            self.ai,
            papers=self.papers if papers is None else papers,
            client=self.client,
            now=NOW,
            **kwargs,
        )

    def test_full_metadata(self):
        self.assertGreater(len(self.paper.abstract), 1000)
        self.assertEqual(self.paper.canonical_id, "arxiv:2609.00001")
        self.assertEqual(self.paper.version, 2)
        self.assertEqual(len(self.paper.authors), 2)
        self.assertEqual(self.paper.categories, ["cs.CL", "cs.IR"])
        self.assertEqual(len(self.paper.links), 2)
        self.assertEqual(self.paper.published, "2026-09-08T12:00:00Z")

    def test_legacy_id_and_api_error(self):
        raw = (
            (FIXTURES / "arxiv.xml").read_text().replace("2609.00001", "hep-th/9901001")
        )
        self.assertEqual(parse_atom(raw)[0].canonical_id, "arxiv:hep-th/9901001")
        with self.assertRaises(ValueError):
            parse_atom(
                raw.replace(
                    "http://arxiv.org/abs/hep-th/9901001v1",
                    "http://arxiv.org/api/errors#incorrect_id_format",
                )
            )
        with self.assertRaises(ValueError):
            parse_atom("<html/>")

    def test_keyword_groups_and_disable(self):
        self.cfg.update(
            keyword_prefilter=True,
            keywords=["absent", "EVIDENCE ATTRIBUTION"],
            required_keywords=["language model"],
        )
        self.assertTrue(prefilter(self.paper, self.cfg))
        self.cfg["required_keywords"] = ["absent"]
        self.assertFalse(prefilter(self.paper, self.cfg))
        self.cfg["keyword_prefilter"] = False
        self.assertTrue(prefilter(self.paper, self.cfg))
        self.cfg.update(keyword_prefilter=True, keywords=[], required_keywords=[])
        self.assertTrue(prefilter(self.paper, self.cfg))

    def test_prefilter_avoids_llm(self):
        self.cfg.update(keyword_prefilter=True, keywords=["not present"])
        self.assertEqual(self.process()["papers"], [])
        self.client.chat.assert_not_called()

    def test_prompt_has_complete_interest_and_abstract(self):
        self.process([self.paper])
        payload = json.loads(self.client.chat.call_args.args[0][-1]["content"])
        self.assertEqual(payload["paper"]["abstract"], self.paper.abstract)
        self.assertEqual(
            payload["research_interest"]["research_question"],
            self.cfg["research_question"],
        )
        self.assertEqual(payload["paper"]["categories"], self.paper.categories)

    def test_threshold_no_fallback_even_boolean_relevant(self):
        self.client.chat.return_value = json.dumps(
            {**self.judgment(0.699), "is_relevant": True}
        )
        result = self.process()
        self.assertEqual(result["papers"], [])
        self.assertEqual(result["status"], "success")
        self.process()
        self.assertEqual(self.client.chat.call_count, 2)  # successful negatives cached

    def test_threshold_inclusive_and_daily_limit(self):
        self.client.chat.return_value = json.dumps(self.judgment(0.7))
        self.cfg["daily_output_limit"] = 1
        self.assertEqual(len(self.process()["papers"]), 1)
        self.cfg["daily_output_limit"] = 0
        self.assertEqual(self.process()["papers"], [])

    def test_cache_isolation(self):
        key = cache_key(self.paper, self.cfg, self.ai)
        for field, value in [
            ("research_question", "Completely different question"),
            ("topics", ["other"]),
            ("keywords", ["other"]),
            ("required_keywords", ["other"]),
            ("relevance_threshold", 0.9),
        ]:
            with self.subTest(field=field):
                self.assertNotEqual(
                    key, cache_key(self.paper, {**self.cfg, field: value}, self.ai)
                )
        self.assertNotEqual(
            key, cache_key(self.paper, self.cfg, {"MODEL": "test/other"})
        )
        self.assertNotEqual(key, cache_key(self.paper, self.cfg, self.ai, "v2"))
        self.assertNotEqual(
            key, cache_key(replace(self.paper, abstract="changed"), self.cfg, self.ai)
        )
        self.assertNotEqual(
            key, cache_key(replace(self.paper, version=3), self.cfg, self.ai)
        )
        self.assertEqual(
            key, cache_key(self.paper, self.cfg, {**self.ai, "API_KEY": "rotated"})
        )

    def test_different_interest_reclassified(self):
        self.process([self.paper])
        self.process([])
        self.assertEqual(self.client.chat.call_count, 1)
        self.cfg["research_question"] = "New research question"
        self.process([])
        self.assertEqual(self.client.chat.call_count, 2)

    def test_dedup_latest_version_retained_across_runs(self):
        result = self.process(self.papers + self.papers)
        self.assertEqual(len(result["papers"]), 2)
        self.assertEqual(self.client.chat.call_count, 2)
        self.process([self.papers[0]])
        self.assertEqual(self.client.chat.call_count, 2)
        state = State(self.cfg["state_path"])
        try:
            self.assertEqual(len(state.papers()), 2)
            self.assertEqual(
                next(
                    p
                    for p in state.papers()
                    if p.canonical_id == self.paper.canonical_id
                ).version,
                2,
            )
        finally:
            state.close()

    def test_failed_processing_retried_not_negative_cached(self):
        self.client.chat.side_effect = [TimeoutError(), json.dumps(self.judgment())]
        self.assertEqual(self.process([self.paper])["status"], "partial_failure")
        result = self.process([])
        self.assertEqual(result["status"], "success")
        self.assertEqual(len(result["papers"]), 1)

    def test_invalid_results_are_failures(self):
        for score in [float("nan"), float("inf"), True, "0.9", -1, 2]:
            with self.subTest(score=score), self.assertRaises(ValueError):
                validate_result(self.judgment(score))
        self.client.chat.return_value = "{}"
        self.assertEqual(self.process([self.paper])["status"], "partial_failure")

    def test_ranking_half_life(self):
        old = replace(
            self.paper,
            canonical_id="arxiv:2609.00003",
            published=(NOW - timedelta(hours=72)).isoformat(),
        )
        new = replace(self.paper, published=NOW.isoformat())
        selected = self.process([old, new])["papers"]
        self.assertEqual(selected[0]["paper"]["canonical_id"], new.canonical_id)
        self.assertAlmostEqual(selected[1]["ranking_score"], 0.85 * 0.8 + 0.15 * 0.5)

    def test_stale_and_future_excluded(self):
        self.assertEqual(
            self.process(
                [replace(self.paper, published=(NOW - timedelta(days=8)).isoformat())]
            )["papers"],
            [],
        )
        self.assertEqual(
            self.process(
                [replace(self.paper, published=(NOW + timedelta(days=1)).isoformat())]
            )["papers"],
            [],
        )
        self.client.chat.assert_not_called()

    def test_fetch_retries_transient_only(self):
        good = Mock(content=(FIXTURES / "arxiv.xml").read_bytes())
        get = Mock(side_effect=[requests.Timeout(), good])
        sleep = Mock()
        self.assertEqual(len(fetch(self.cfg, NOW, get=get, sleep=sleep)), 3)
        sleep.assert_called_once_with(3)
        self.assertIn("submittedDate", get.call_args.kwargs["params"]["search_query"])
        bad = requests.HTTPError(response=Mock(status_code=400))
        get = Mock(side_effect=bad)
        with self.assertRaises(requests.HTTPError):
            fetch(self.cfg, NOW, get=get, sleep=sleep)
        self.assertEqual(get.call_count, 1)

    def test_ingestion_failure_uses_retained_candidates(self):
        self.process([self.paper])
        with patch("trendradar.papers.pipeline.fetch", side_effect=requests.Timeout()):
            result = run(self.cfg, self.ai, client=self.client, now=NOW)
        self.assertEqual(len(result["papers"]), 1)
        self.assertEqual(result["status"], "partial_failure")

    def test_disabled_no_network(self):
        with patch("trendradar.papers.pipeline.run") as runner, patch("trendradar.papers.pipeline.load", return_value={"enabled": False}):
            self.assertIsNone(
                daily_recommendations({"PAPERS_CONFIG_PATH": "config/papers.yaml"}, NOW)
            )
            self.assertIsNone(daily_recommendations({}, NOW))
            runner.assert_not_called()

    def test_invalid_config(self):
        for field, value in [
            ("keywords", "wrong"),
            ("relevance_threshold", 1.1),
            ("recency_half_life_hours", 0),
            ("daily_output_limit", -1),
            ("keyword_prefilter", "false"),
        ]:
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate({field: value})

    def test_html_escaping_and_empty(self):
        result = self.process([replace(self.paper, title="<script>alert(1)</script>")])
        html = append_section("<body>NEWS</body>", result)
        self.assertIn("NEWS", html)
        self.assertIn("Paper Recommendations", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>", html)
        self.assertIn(self.paper.abstract, html)
        self.assertIn(
            "No papers met", append_section("<body/>", {"papers": [], "errors": []})
        )
        self.assertEqual(append_section("original", None), "original")

    def test_existing_report_and_email_mime(self):
        from trendradar.notification.senders import send_to_email
        from trendradar.report.generator import generate_html_report
        from trendradar.report.html import render_html_content

        result = self.process([self.paper])
        previous = Path.cwd()
        try:
            os.chdir(self.temp.name)
            path = generate_html_report(
                [],
                0,
                date_folder="2026-09-09",
                time_filename="12-00",
                render_html_func=render_html_content,
                report_metadata={"paper_recommendations": result},
                new_titles={"news": {self.paper.title: {}}},
                id_to_name={"news": "News"},
            )
            html = Path(path).read_text()
            self.assertIn(
                self.paper.title, html
            )  # survives prepare_report_data's title pruning
            self.assertIn(self.paper.abstract, html)
            for copy_path in (
                "output/index.html",
                "index.html",
                "output/html/latest/daily.html",
            ):
                self.assertEqual(Path(copy_path).read_text(), html)
            with patch("trendradar.notification.senders.smtplib.SMTP") as smtp:
                self.assertTrue(
                    send_to_email(
                        "sender@example.test",
                        "fake",
                        "reader@example.test",
                        "daily",
                        path,
                        "smtp.example.test",
                        587,
                        get_time_func=lambda: NOW,
                    )
                )
                message = smtp.return_value.send_message.call_args.args[0]
                body = next(
                    part
                    for part in message.walk()
                    if part.get_content_type() == "text/html"
                )
                self.assertEqual(body.get_payload(decode=True).decode(), html)
        finally:
            os.chdir(previous)

    def test_papers_survive_title_classifier_and_force_html(self):
        from trendradar.__main__ import NewsAnalyzer

        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer._paper_result = self.process([self.paper])
        analyzer.ctx = Mock()
        analyzer.ctx.config = {
            "STORAGE": {"FORMATS": {"HTML": False}},
            "SHOW_VERSION_UPDATE": False,
        }
        analyzer.ctx.display_mode = "keyword"
        analyzer.ctx.platform_ids = []
        analyzer.ctx.run_ai_filter.return_value = Mock(
            success=True, total_matched=0, tags=[]
        )
        analyzer.ctx.convert_ai_filter_to_report_data.return_value = ([], [], [])
        analyzer.filter_method = "ai"
        analyzer.interests_file = "news-only.txt"
        analyzer.frequency_file = None
        analyzer._rss_total_count = analyzer._rss_source_total = (
            analyzer._rss_source_failed
        ) = 0
        analyzer._run_analysis_pipeline({}, "daily", {}, {}, [], [], {})
        analyzer.ctx.run_ai_filter.assert_called_once_with(
            interests_file="news-only.txt"
        )
        metadata = analyzer.ctx.generate_html.call_args.kwargs["report_metadata"]
        self.assertEqual(metadata["paper_recommendations"], analyzer._paper_result)

    def test_papers_only_pass_notification_gate_and_obey_schedule(self):
        from trendradar.__main__ import NewsAnalyzer

        analyzer = NewsAnalyzer.__new__(NewsAnalyzer)
        analyzer._paper_result = self.process([self.paper])
        analyzer.ctx = Mock()
        analyzer.ctx.config = {
            "ENABLE_NOTIFICATION": True,
            "SHOW_VERSION_UPDATE": False,
            "EMAIL_FROM": "sender@example.test",
            "EMAIL_PASSWORD": "fake",
            "EMAIL_TO": "reader@example.test",
        }
        analyzer.ctx.platform_ids = []
        analyzer.ctx.prepare_report.return_value = {}
        analyzer.ctx.create_notification_dispatcher.return_value.dispatch_all.return_value = {
            "email": True
        }
        analyzer._has_notification_configured = Mock(return_value=True)
        analyzer.report_mode = "daily"
        analyzer.frequency_file = None
        analyzer.proxy_url = None
        analyzer._hotlist_total_count = analyzer._rss_matched_count = 0
        analyzer._rss_total_count = analyzer._rss_source_total = (
            analyzer._rss_source_failed
        ) = 0
        schedule = Mock(push=True, once_push=False)
        self.assertTrue(
            analyzer._send_notification_if_needed(
                [], "daily", "daily", schedule=schedule
            )
        )
        schedule.push = False
        self.assertFalse(
            analyzer._send_notification_if_needed(
                [], "daily", "daily", schedule=schedule
            )
        )
        analyzer.ctx.create_notification_dispatcher.return_value._send_email.assert_called_once()
        analyzer.ctx.create_notification_dispatcher.return_value.dispatch_all.assert_not_called()
        schedule.push = True
        analyzer.ctx.config["EMAIL_TO"] = ""
        self.assertFalse(
            analyzer._send_notification_if_needed(
                [], "daily", "daily", schedule=schedule
            )
        )

    def test_shared_client_retries_config_and_no_fallback(self):
        with patch("trendradar.ai.client.AIClient") as client_class:
            client_class.return_value.chat.return_value = json.dumps(self.judgment())
            result = run(
                self.cfg,
                {**self.ai, "FALLBACK_MODELS": ["test/other"]},
                papers=[self.paper],
                now=NOW,
            )
        self.assertEqual(len(result["papers"]), 1)
        self.assertEqual(client_class.call_args.args[0]["NUM_RETRIES"], 2)
        self.assertEqual(client_class.call_args.args[0]["FALLBACK_MODELS"], [])
