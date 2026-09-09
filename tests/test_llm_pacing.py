import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from trendradar.content_pool.llm import Meter, ModelCoolingDown
from trendradar.content_pool.store import Store


class Clock:
    def __init__(self):
        self.value = 1000
        self.waits = []

    def time(self):
        return self.value

    def sleep(self, n):
        self.waits.append(n)
        self.value += n


class Client:
    def __init__(self, *args):
        pass

    def chat(self, *args, **kwargs):
        return "{}"


class PacingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "pool.db"
        self.store = Store(self.path)
        self.ai = {"MODEL": "test"}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_spacing_across_stages_and_restarts(self):
        clock = Clock()
        with patch("trendradar.content_pool.llm.time", clock):
            Meter(self.store, self.ai, {"min_request_interval": 30}, Client).chat(
                "classification", []
            )
            Meter(self.store, self.ai, {"min_request_interval": 30}, Client).chat(
                "paper", []
            )
        self.assertEqual(clock.waits, [30])

    def test_rate_limit_cooldown_and_exponential_backoff(self):
        class RateLimitError(Exception):
            status_code = 429

        class Limited(Client):
            def chat(self, *args, **kwargs):
                raise RateLimitError()

        clock = Clock()
        with patch("trendradar.content_pool.llm.time", clock):
            first = Meter(self.store, self.ai, {"min_request_interval": 30}, Limited)
            with self.assertRaises(ModelCoolingDown):
                first.chat("classification", [])
            second = Meter(self.store, self.ai, {"min_request_interval": 30}, Limited)
            with self.assertRaises(ModelCoolingDown):
                second.chat("summary", [])
            self.assertEqual(second.calls, 0)
            clock.value += 300
            with self.assertRaises(ModelCoolingDown):
                second.chat("summary", [])
            self.assertEqual(
                self.store.db.execute("SELECT not_before FROM llm_limits").fetchone()[
                    0
                ],
                clock.value + 600,
            )

    def test_non_rate_error_waits_before_retry(self):
        clock = Clock()
        calls = []

        class Flaky(Client):
            def chat(self, *args, **kwargs):
                calls.append(1)
                if len(calls) == 1:
                    raise OSError()
                return "{}"

        with patch("trendradar.content_pool.llm.time", clock):
            Meter(
                self.store,
                self.ai,
                {"min_request_interval": 30, "retry_delay": 60},
                Flaky,
            ).chat("paper", [])
        self.assertEqual(clock.waits, [60])

    def test_two_connections_never_call_simultaneously(self):
        active = 0
        peak = 0
        guard = threading.Lock()

        class Slow(Client):
            def chat(self, *args, **kwargs):
                nonlocal active, peak
                with guard:
                    active += 1
                    peak = max(peak, active)
                time.sleep(0.02)
                with guard:
                    active -= 1
                return "{}"

        def run(_):
            store = Store(self.path)
            try:
                Meter(store, self.ai, {}, Slow).chat("paper", [])
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(run, range(2)))
        self.assertEqual(peak, 1)

    def test_summary_output_budget_uses_same_model(self):
        limits = []

        class Capture(Client):
            def chat(self, *args, **kwargs):
                limits.append(kwargs["max_tokens"])
                return "{}"

        meter = Meter(
            self.store,
            self.ai,
            {"max_output_tokens": 3000, "summary_max_output_tokens": 8000},
            Capture,
        )
        meter.chat("classification", [])
        meter.chat("summary", [])
        self.assertEqual(limits, [3000, 8000])
        self.assertEqual(meter.model("classification"), meter.model("summary"))
