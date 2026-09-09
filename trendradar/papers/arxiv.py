"""arXiv Atom adapter. Does not pass abstracts through the RSS summary parser."""

import re
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

import requests

ATOM = "{http://www.w3.org/2005/Atom}"


def date(value):
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError("paper timestamps must include a timezone")
    return stamp.astimezone(UTC)


@dataclass(frozen=True)
class Paper:
    canonical_id: str
    version: int
    title: str
    abstract: str
    authors: list[str]
    categories: list[str]
    published: str
    updated: str
    url: str
    links: list[dict]

    def to_dict(self):
        return asdict(self)


def parse_atom(raw):
    root = ET.fromstring(raw)
    if root.tag != ATOM + "feed":
        raise ValueError("expected an arXiv Atom feed")
    papers = []
    for entry in root.findall(ATOM + "entry"):

        def text(name, entry=entry):
            return (entry.findtext(ATOM + name) or "").strip()

        match = re.fullmatch(
            r"https?://arxiv.org/abs/((?:\d{4}\.\d{4,5}|[a-zA-Z.-]+/\d{7}))(?:v(\d+))?",
            text("id"),
        )
        if not match:
            raise ValueError("invalid arXiv entry ID (possibly an API error response)")
        identity, version = match.group(1), int(match.group(2) or 1)
        if not text("title") or not text("summary"):
            raise ValueError(f"missing title or abstract for {identity}")
        date(text("published"))
        date(text("updated"))
        papers.append(
            Paper(
                canonical_id=f"arxiv:{identity}",
                version=version,
                title=" ".join(text("title").split()),
                abstract=text("summary"),
                authors=[
                    (a.findtext(ATOM + "name") or "").strip()
                    for a in entry.findall(ATOM + "author")
                ],
                categories=[c.attrib["term"] for c in entry.findall(ATOM + "category")],
                published=text("published"),
                updated=text("updated"),
                url=f"https://arxiv.org/abs/{identity}",
                links=[dict(link.attrib) for link in entry.findall(ATOM + "link")],
            )
        )
    return papers


def fetch(cfg, now, get=requests.get, sleep=time.sleep):
    """One bounded request, sorted newest first; retry network/429/5xx errors."""
    start = (now - timedelta(days=cfg["lookback_days"])).strftime("%Y%m%d%H%M")
    end = now.strftime("%Y%m%d%H%M")
    params = {
        "search_query": f"({cfg['arxiv_query']}) AND submittedDate:[{start} TO {end}]",
        "start": 0,
        "max_results": cfg["max_candidates"],
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    for attempt in range(3):
        try:
            response = get(
                "https://export.arxiv.org/api/query",
                params=params,
                timeout=cfg["request_timeout"],
                headers={"User-Agent": "TrendRadar-Papers/1.0"},
            )
            response.raise_for_status()
            return parse_atom(response.content)
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if attempt == 2 or (status is not None and status != 429 and status < 500):
                raise
            sleep(3 * (2**attempt))
