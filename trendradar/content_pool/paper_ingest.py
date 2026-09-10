"""Put official paper metadata into the same raw candidate pool as RSS articles."""

import json

from trendradar.papers.arxiv import Paper

from .store import atomic, dumps, ingest, now


def ingest_papers(db, papers, *, source_id="arxiv", history=False):
    count = 0
    with atomic(db):
        for paper in papers:
            if not paper.canonical_id or not paper.abstract.strip():
                raise ValueError("canonical_identity_and_complete_abstract_required")
            db.execute(
                """INSERT INTO papers VALUES (?,?,?)
                ON CONFLICT(id) DO UPDATE SET version=excluded.version,content=excluded.content
                WHERE excluded.version >= papers.version""",
                (paper.canonical_id, paper.version, json.dumps(paper.to_dict(), ensure_ascii=False)),
            )
            # A stale feed must not replace a newer retained paper in candidates.
            paper = Paper(**json.loads(db.execute(
                "SELECT content FROM papers WHERE id=?", (paper.canonical_id,)
            ).fetchone()[0]))
            existing = db.execute(
                "SELECT 1 FROM records WHERE platform='paper' AND content_id=?",
                (paper.canonical_id,),
            ).fetchone()
            ingest(db, source_id, [{
                "platform": "paper", "content_id": paper.canonical_id,
                "url": paper.url, "title": paper.title,
                "author": ", ".join(paper.authors), "body_html": "",
                "body_text": paper.abstract, "published_at": paper.published,
                "quality_flags": "[]", "raw_entry": dumps(paper.to_dict()),
            }], now())
            if not existing:
                if history:
                    db.execute(
                        "UPDATE records SET history=1 WHERE platform='paper' AND content_id=?",
                        (paper.canonical_id,),
                    )
                count += 1
    return count
