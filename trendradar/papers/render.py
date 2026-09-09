"""Shared HTML section for daily reports, email and offline previews."""

import re
from html import escape
from urllib.parse import urlsplit


def render_section(result):
    if result is None:
        return ""
    parts = [
        '<section id="paper-recommendations" style="margin:24px;padding:20px;border:1px solid #ddd">',
        "<h2>Paper Recommendations</h2>",
    ]
    if result.get("errors"):
        parts.append(
            "<p>Paper processing was incomplete; failed items will be retried on the next run.</p>"
        )
    elif not result["papers"]:
        parts.append("<p>No papers met the configured research criteria.</p>")
    for item in result["papers"]:
        paper = item["paper"]
        url = paper["url"]
        if urlsplit(url).scheme not in ("http", "https"):
            url = "#"
        parts.extend(
            [
                f'<article><h3><a href="{escape(url, quote=True)}">{escape(paper["title"])}</a></h3>',
                f"<p>{escape(', '.join(paper['authors']))} · {escape(paper['published'])}<br>",
                f"{escape(paper['canonical_id'])} v{paper['version']} · {escape(', '.join(paper['categories']))}</p>",
                f"<p><strong>Relevance:</strong> {item['relevance_score']:.2f} / 1<br>",
                f"<strong>Research topics:</strong> {escape(', '.join(item['research_topics']))}</p>",
                f"<p><strong>Why it matters to your research (abstract-based):</strong> {escape(item['reason'])}</p>",
                f'<p style="white-space:pre-wrap"><strong>Full abstract:</strong> {escape(paper["abstract"])}</p></article>',
            ]
        )
    parts.append("</section>")
    return "\n".join(parts)


def append_section(html, result):
    section = render_section(result)
    if not section:
        return html
    # Insert before the final body close; all report snapshots receive the section.
    matches = list(re.finditer(r"</body\s*>", html, re.IGNORECASE))
    if matches:
        pos = matches[-1].start()
        return html[:pos] + section + html[pos:]
    return html + section
