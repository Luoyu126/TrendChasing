"""Single-model, serial calls with persistent pacing, cooldowns and usage budgets."""

import fcntl
import json
import time
import uuid
from pathlib import Path

from .store import digest, dumps, now

DEFAULTS = {
    "batch_size": 10,
    "input_limit": 16000,
    "run_input_budget": 200000,
    "run_call_budget": 100,
    "max_output_tokens": 3000,
    "retries": 1,
    "max_topics": 6,
    "prompt_version": "social-v1",
    "rules_version": "clean-v1",
    "summary_version": "topics-v1",
    "min_request_interval": 0,
    "retry_delay": 0,
    "cooldown_seconds": 300,
}


class BudgetExceeded(RuntimeError):
    pass


class ModelCoolingDown(RuntimeError):
    pass


class Meter:
    def __init__(self, store, ai, config=None, factory=None):
        self.store, self.ai = store, ai
        self.config = {**DEFAULTS, **(config or {})}
        for key in (
            "batch_size",
            "input_limit",
            "run_input_budget",
            "run_call_budget",
            "max_output_tokens",
            "max_topics",
        ):
            if type(self.config[key]) is not int or self.config[key] < 1:
                raise ValueError(key + " must be a positive integer")
        if (
            type(self.config["retries"]) is not int
            or not 0 <= self.config["retries"] <= 3
        ):
            raise ValueError("retries must be between 0 and 3")
        for key in ("min_request_interval", "retry_delay", "cooldown_seconds"):
            if (
                type(self.config[key]) not in (int, float)
                or not 0 <= self.config[key] <= 3600
            ):
                raise ValueError("invalid_timing:" + key)
        if "summary_max_output_tokens" in self.config and (
            type(self.config["summary_max_output_tokens"]) is not int
            or self.config["summary_max_output_tokens"] < 1
        ):
            raise ValueError("invalid_summary_output_limit")
        self.factory = factory
        self.run_id = digest(["daily-budget", self.config["report_date"]]) if self.config.get("report_date") else uuid.uuid4().hex
        self.calls = self.inputs = 0
        self.client = None
        self.group = digest([ai.get("API_BASE"), ai.get("API_KEY")])
        self.store.db.execute(
            "CREATE TABLE IF NOT EXISTS llm_limits (quota_group TEXT PRIMARY KEY,not_before REAL NOT NULL)"
        )
        self.store.db.execute(
            "CREATE TABLE IF NOT EXISTS llm_backoff (quota_group TEXT PRIMARY KEY,failures INTEGER NOT NULL)"
        )
        self.store.db.commit()
        if self.config.get("report_date"):
            row = self.store.db.execute(
                "SELECT count(*),COALESCE(sum(estimated_input),0) FROM usage_events WHERE run_id=? AND event='request_started'",
                (self.run_id,),
            ).fetchone()
            self.calls, self.inputs = row[0], row[1]

    def model(self, stage):
        return self.ai.get("MODEL", "deepseek/deepseek-chat")

    def identity(self, stage):
        return {
            k: v
            for k, v in self.ai.items()
            if k not in ("API_KEY", "TIMEOUT", "NUM_RETRIES", "FALLBACK_MODELS")
        }

    def scoring_config(self):
        return self.ai

    def event(
        self, stage, event, estimate=0, usage=None, cost=None, error=None, attempt=0
    ):
        usage = usage or {}
        with self.store.db:
            self.store.db.execute(
                """INSERT INTO usage_events
              (run_id,stage,model,event,attempt,estimated_input,input_tokens,output_tokens,cost,error,created_at)
              VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    self.run_id,
                    stage,
                    self.model(stage),
                    event,
                    attempt,
                    estimate,
                    usage.get("input_tokens"),
                    usage.get("output_tokens"),
                    cost,
                    error,
                    now(),
                ),
            )

    def cached(self, stage, key):
        value = self.store.cache_get(key)
        if value is not None:
            self.event(stage, "cache_hit")
        return value

    @staticmethod
    def estimate(messages):
        return len(dumps(messages).encode()) + 64

    def defer(self, seconds):
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO llm_limits VALUES (?,?) ON CONFLICT(quota_group) DO UPDATE SET not_before=max(not_before,excluded.not_before)",
                (self.group, time.time() + seconds),
            )

    def chat(self, stage, messages, _retry=0, **kwargs):
        estimate = self.estimate(messages)
        # All stages and separate CLI processes using this database share one gate.
        with Path(self.store.path).with_suffix(".llm.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            for attempt in range(self.config["retries"] + 1):
                if (
                    estimate > self.config["input_limit"]
                    or self.calls >= self.config["run_call_budget"]
                    or self.inputs + estimate > self.config["run_input_budget"]
                ):
                    raise BudgetExceeded("input_or_call_budget_exhausted")
                row = self.store.db.execute(
                    "SELECT not_before FROM llm_limits WHERE quota_group=?",
                    (self.group,),
                ).fetchone()
                wait = max(0, row[0] - time.time()) if row else 0
                # Do not loop through candidates on a throttled account. Resume on
                # a later run once its persisted cooldown expires.
                if (
                    wait
                    > max(
                        self.config["min_request_interval"],
                        self.config["retry_delay"] * (2**attempt),
                    )
                    + 1
                ):
                    self.event(stage, "cooldown_skip")
                    raise ModelCoolingDown("model_account_cooling_down")
                if wait:
                    time.sleep(wait)
                if self.client is None:
                    factory = self.factory
                    if factory is None:
                        from trendradar.ai.client import AIClient

                        factory = AIClient
                    self.client = factory(
                        {**self.ai, "NUM_RETRIES": 0, "FALLBACK_MODELS": []}
                    )
                # Reserve budget durably before calling the provider. On a crash,
                # an uncertain request still consumes this business day's budget.
                self.event(stage, "request_started", estimate, attempt=attempt + _retry)
                self.calls += 1
                self.inputs += estimate
                try:
                    result = self.client.chat(
                        messages,
                        **{
                            **kwargs,
                            "num_retries": 0,
                            "max_tokens": self.config.get(
                                "summary_max_output_tokens",
                                self.config["max_output_tokens"],
                            )
                            if stage == "summary"
                            else self.config["max_output_tokens"],
                        },
                    )
                    usage = getattr(self.client, "last_usage", None)
                    cost = getattr(self.client, "last_cost", None)
                    prices = self.config.get("prices", {}).get(self.model(stage))
                    if (
                        cost is None
                        and prices
                        and usage
                        and all(
                            usage.get(k) is not None
                            for k in ("input_tokens", "output_tokens")
                        )
                    ):
                        cost = sum(
                            usage[k] * prices[k] / 1000000
                            for k in ("input_tokens", "output_tokens")
                        )
                    self.event(
                        stage,
                        "request",
                        estimate,
                        usage,
                        cost,
                        attempt=attempt + _retry,
                    )
                    with self.store.db:
                        self.store.db.execute(
                            "DELETE FROM llm_backoff WHERE quota_group=?", (self.group,)
                        )
                    self.defer(self.config["min_request_interval"])
                    return result
                except Exception as exc:
                    self.event(
                        stage,
                        "request_failed",
                        estimate,
                        error=type(exc).__name__,
                        attempt=attempt + _retry,
                    )
                    self.defer(self.config["min_request_interval"])
                    body = getattr(exc, "body", None)
                    code = (
                        str(body.get("error", {}).get("code", ""))
                        if isinstance(body, dict)
                        and isinstance(body.get("error"), dict)
                        else ""
                    )
                    if (
                        getattr(exc, "status_code", None) == 429
                        or type(exc).__name__ == "RateLimitError"
                        or code in ("1302", "1305")
                    ):
                        with self.store.db:
                            self.store.db.execute(
                                "INSERT INTO llm_backoff VALUES (?,1) ON CONFLICT(quota_group) DO UPDATE SET failures=failures+1",
                                (self.group,),
                            )
                            failures = self.store.db.execute(
                                "SELECT failures FROM llm_backoff WHERE quota_group=?",
                                (self.group,),
                            ).fetchone()[0]
                        delay = min(
                            3600,
                            self.config["cooldown_seconds"]
                            * (2 ** min(failures - 1, 5)),
                        )
                        try:
                            retry_after = float(
                                getattr(
                                    getattr(exc, "response", None), "headers", {}
                                ).get("retry-after", 0)
                            )
                            delay = max(delay, min(3600, retry_after))
                        except (TypeError, ValueError):
                            pass
                        self.defer(delay)
                        raise ModelCoolingDown("model_account_cooling_down") from None
                    if attempt == self.config["retries"]:
                        raise
                    self.defer(min(3600, self.config["retry_delay"] * (2**attempt)))

    def json(self, stage, prompt, value, retry=0):
        return json.loads(
            self.chat(
                stage,
                [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": dumps(value)},
                ],
                temperature=0,
                response_format={"type": "json_object"},
                _retry=retry,
            )
        )
