"""Research configuration is independent of news interests and model secrets."""

import math
from pathlib import Path

import yaml

DEFAULTS = {
    "enabled": False,
    "research_question": "",
    "topics": [],
    "keywords": [],
    "required_keywords": [],
    "keyword_prefilter": False,
    "relevance_threshold": 0.7,
    "daily_output_limit": 10,
    "relevance_weight": 0.85,
    "recency_weight": 0.15,
    "recency_half_life_hours": 72,
    "lookback_days": 7,
    "arxiv_query": "cat:cs.AI OR cat:cs.LG",
    "max_candidates": 200,
    "request_timeout": 30,
    "state_path": "output/content_pool/content.sqlite3",
}


def validate(raw):
    cfg = {**DEFAULTS, **raw}
    for key in ("enabled", "keyword_prefilter"):
        if not isinstance(cfg[key], bool):
            raise ValueError(f"{key} must be a boolean")  # noqa: TRY004 - unified configuration/result validation
    for key in ("topics", "keywords", "required_keywords"):
        if not isinstance(cfg[key], list) or any(
            not isinstance(v, str) or not v.strip() for v in cfg[key]
        ):
            raise ValueError(f"{key} must be a list of nonempty strings")
    for key in ("research_question", "arxiv_query", "state_path"):
        if not isinstance(cfg[key], str) or (cfg["enabled"] and not cfg[key].strip()):
            raise ValueError(f"{key} must be a nonempty string when enabled")
    for key in (
        "relevance_threshold",
        "relevance_weight",
        "recency_weight",
        "recency_half_life_hours",
        "lookback_days",
        "request_timeout",
    ):
        value = cfg[key]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError(f"{key} must be a finite nonnegative number")
    if cfg["relevance_threshold"] > 1:
        raise ValueError("relevance_threshold must be in [0, 1]")
    if cfg["relevance_weight"] + cfg["recency_weight"] <= 0:
        raise ValueError("ranking weights must have a positive sum")
    for key in ("recency_half_life_hours", "lookback_days", "request_timeout"):
        if cfg[key] <= 0:
            raise ValueError(f"{key} must be positive")
    for key in ("daily_output_limit", "max_candidates"):
        if type(cfg[key]) is not int or cfg[key] < 0:
            raise ValueError(f"{key} must be a nonnegative integer")
    if not 1 <= cfg["max_candidates"] <= 2000:
        raise ValueError("max_candidates must be between 1 and 2000")
    return cfg


def load(path):
    with Path(path).open(encoding="utf-8") as source:
        raw = yaml.safe_load(source) or {}
    if not isinstance(raw, dict):
        raise ValueError("paper configuration must be a mapping")  # noqa: TRY004 - unified configuration validation
    # The sibling pool config is authoritative unless a standalone fixture uses
    # an explicit isolated state_path. Paths are repository-relative.
    if "state_path" not in raw:
        pool_path = Path(path).resolve().with_name("content_pool.yaml")
        if pool_path.exists():
            pool = yaml.safe_load(pool_path.read_text())
            raw["state_path"] = str((pool_path.parent.parent / pool["db_path"]).resolve())
    return validate(raw)
