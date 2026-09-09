"""Local configuration and mutually exclusive operational runs."""

import contextlib
import fcntl
import os
import re
from pathlib import Path

import yaml


def load_env(path):
    """Read literal dotenv assignments, never execute shell code or log values."""
    path = Path(path)
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.removeprefix("export ").partition("=")
        if not sep or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key.strip()):
            raise ValueError("invalid_environment_assignment")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if not os.environ.get(key.strip()):
            os.environ[key.strip()] = value


def configuration(root, path):
    root = Path(root)
    for filename in ("ai.local.env", "email.local.env"):
        load_env(root / "config" / filename)
    cfg = yaml.safe_load(Path(path).read_text())
    for key in (
        "db_path",
        "papers_config",
        "ai_config",
        "report_dir",
        "sources_config",
    ):
        if key in cfg:
            cfg[key] = str((root / cfg[key]).resolve())
    from trendradar.core.loader import _load_ai_config
    from trendradar.papers.config import load

    ai = _load_ai_config(yaml.safe_load(Path(cfg["ai_config"]).read_text()))
    paper = load(cfg["papers_config"])
    paper["state_path"] = str((root / paper["state_path"]).resolve())
    return cfg, ai, paper


@contextlib.contextmanager
def run_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another_content_run_is_active") from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)
