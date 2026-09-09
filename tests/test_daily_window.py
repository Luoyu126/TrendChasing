import unittest
from datetime import datetime
from unittest.mock import patch

import test_pool_workflow as workflow
from test_pool_workflow import item

from trendradar.content_pool.window import position, previous_day


class CalendarTests(unittest.TestCase):
    def test_pittsburgh_boundaries(self):
        w = previous_day("2026-09-10T12:00:00+00:00", "America/New_York")
        self.assertEqual(w["date"], "2026-09-09")
        self.assertEqual(w["start"], "2026-09-09T04:00:00+00:00")
        self.assertEqual(w["end"], "2026-09-10T04:00:00+00:00")
        self.assertEqual(position(w["start"], w), "primary")
        self.assertEqual(position(w["end"], w), "future")
        self.assertEqual(position("2026-09-09T03:59:59Z", w), "supplement")
        self.assertEqual(position(None, w), "unknown")

    def test_dst_short_and_long_days(self):
        for now, hours in [("2026-03-09T12:00:00Z", 23), ("2026-11-02T13:00:00Z", 25)]:
            w = previous_day(now, "America/New_York")
            duration = datetime.fromisoformat(w["end"]) - datetime.fromisoformat(
                w["start"]
            )
            self.assertEqual(duration.total_seconds() / 3600, hours)


class DailySelectionTests(unittest.TestCase):
    setUp = workflow.Workflow.setUp
    tearDown = workflow.Workflow.tearDown
    ingest = workflow.Workflow.ingest
    count = workflow.Workflow.count

    def test_future_and_unknown_retained_supplements_labeled(self):
        self.meter.config.update(
            daily_window="previous_day", timezone="America/New_York"
        )
        self.ingest(
            dict(item("yesterday"), published_at="2026-09-09T10:00:00Z"),
            dict(item("older"), published_at="2026-09-08T10:00:00Z"),
            dict(item("today"), published_at="2026-09-10T04:00:00Z"),
            dict(item("unknown"), published_at=None),
        )
        with patch(
            "trendradar.content_pool.store.now",
            return_value="2026-09-10T12:00:00+00:00",
        ):
            self.processor.process()
        self.assertEqual(self.count("pool_entries"), 2)
        with patch(
            "trendradar.content_pool.digest.now",
            return_value="2026-09-10T12:00:00+00:00",
        ):
            batch = self.digests.prepare()
        result = self.digests.preview(batch)
        sources = {s["content_id"]: s for r in result["entries"] for s in r["sources"]}
        self.assertFalse(sources["yesterday"]["supplement"])
        self.assertTrue(sources["older"]["supplement"])
        self.assertEqual(result["window"]["date"], "2026-09-09")
        self.assertEqual(self.count("items"), 4)
