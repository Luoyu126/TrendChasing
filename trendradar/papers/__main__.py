"""Offline fixture preview by default; this command has no notification calls."""

import argparse
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from .arxiv import parse_atom
from .config import load
from .pipeline import run
from .render import append_section


def main():
    parser = argparse.ArgumentParser(
        description="Offline paper pipeline preview (no network or email)"
    )
    parser.add_argument("--config", default="config/papers.yaml")
    parser.add_argument("--fixture", required=True, help="arXiv Atom XML")
    parser.add_argument(
        "--scores", required=True, help="JSON judgments keyed by canonical ID"
    )
    parser.add_argument("--now", required=True, help="ISO timestamp with timezone")
    parser.add_argument("--output", default="output/papers/preview.html")
    args = parser.parse_args()
    scores = json.loads(Path(args.scores).read_text(encoding="utf-8"))

    class FixtureClient:
        def chat(self, messages, **kwargs):
            paper = json.loads(messages[-1]["content"])["paper"]
            return json.dumps(scores[paper["canonical_id"]])

    now = datetime.fromisoformat(args.now)
    if now.tzinfo is None:
        parser.error("--now must include a timezone")
    cfg = load(args.config)
    with tempfile.TemporaryDirectory() as directory:
        cfg["state_path"] = str(Path(directory) / "state.sqlite3")
        result = run(
            cfg,
            {"MODEL": "offline/fixture"},
            papers=parse_atom(Path(args.fixture).read_bytes()),
            client=FixtureClient(),
            now=now.astimezone(UTC),
        )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        append_section(
            "<html><body><h1>Offline daily email preview</h1></body></html>", result
        ),
        encoding="utf-8",
    )
    output.with_suffix(".json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"{len(result['papers'])} selected; {len(result['errors'])} errors; preview: {output}"
    )
    if result["errors"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
