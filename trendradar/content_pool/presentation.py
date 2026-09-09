"""Email-friendly HTML, using existing structured insights without extra LLM calls."""

from datetime import datetime
from html import escape
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

PLATFORMS = {
    "wechat": "微信公众号",
    "twitter": "X",
    "zhihu": "知乎",
    "xiaohongshu": "小红书",
    "paper": "arXiv / 论文来源",
}


def render(result, timezone="Asia/Shanghai", link_mode="full"):
    if link_mode not in ("full", "plain_address"):
        raise ValueError("invalid_email_link_mode")

    def link(url, label):
        if link_mode == "plain_address":
            address = re.sub(r"https?://", "", url, flags=re.IGNORECASE)
            return f'{escape(label)} <span style="overflow-wrap:anywhere">（{escape(address)}）</span>'
        safe = url if urlsplit(url).scheme in ("http", "https") else "#"
        return f'<a style="color:#245ac5;text-decoration:none" href="{escape(safe, quote=True)}">{escape(label)}</a>'

    date = (
        datetime.fromisoformat(result["created_at"])
        .astimezone(ZoneInfo(timezone))
        .strftime("%Y 年 %m 月 %d 日")
    )
    coverage = result.get("window")
    if coverage:
        date += " · 内容日期 " + coverage["date"] + "（匹兹堡时间）"
    entries = result["entries"]
    papers = [e for e in entries if e["category"] == "low_level"]
    high = [e for e in entries if e["category"] == "high_level"]
    by_id = {r["id"]: r for r in entries}
    lines = [
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"></head>',
        "<body style=\"margin:0;background:#f4f6f9;color:#253044;font-family:Arial,'Microsoft YaHei',sans-serif;line-height:1.7\">",
        '<div style="max-width:760px;margin:24px auto;background:white;padding:28px;border-radius:12px">',
        '<h1 style="margin:0;color:#142238">AI 日报</h1>',
        f'<p style="color:#788397">{date} · {len(high)} 条观点与时事 · {len(papers)} 篇论文</p>',
        '<h2 style="border-bottom:2px solid #e6ecf4;padding-bottom:10px">一、观点与时事</h2>',
    ]
    if not high:
        lines.append('<p style="color:#788397">本期暂无新增内容。</p>')
    for topic in result["high_level"]:
        lines += [
            f"<h3>{escape(topic['title'])}</h3>",
            f"<p>{escape(topic['summary'])}</p>",
            '<ul style="padding-left:20px">',
        ]
        for identity in topic["ids"]:
            for source in by_id[identity]["sources"]:
                author = ("【补充】" if source.get("supplement") else "") + (
                    source["author"] or "作者未提供"
                )
                platform = PLATFORMS.get(source["platform"], source["platform"])
                lines.append(
                    f'<li style="margin-bottom:12px"><strong>{escape(author)}</strong> · {escape(platform)}：{escape(source["insight"])} {link(source["url"], "原文")}</li>'
                )
        lines.append("</ul>")
    lines.append(
        '<h2 style="border-bottom:2px solid #e6ecf4;padding-bottom:10px">二、精选论文</h2>'
    )
    if not papers:
        lines.append('<p style="color:#788397">本期暂无通过研究兴趣筛选的新论文。</p>')
    for entry in papers:
        p = entry["payload"]
        paper = p["paper"]
        (
            "【补充】"
            if entry["sources"] and all(s.get("supplement") for s in entry["sources"])
            else ""
        ) + paper["title"]
        lines += [
            f"<h3>{link(paper['url'], paper['title'])}</h3>",
            f'<p style="font-size:13px;color:#788397">{escape(", ".join(paper["authors"]))} · {escape(paper["published"][:10])}</p>',
            f"<p><strong>为什么值得读：</strong>{escape(p['reason'])}</p>",
        ]
        for source in entry["sources"]:
            if source["platform"] == "paper":
                continue
            lines.append(
                f'<p style="font-size:14px"><strong>{escape(source["author"])}</strong> · {escape(PLATFORMS.get(source["platform"], source["platform"]))}：{escape(source["insight"])} {link(source["url"], "解读原文")}</p>'
            )
    if result.get("notice"):
        lines.append(
            f'<p style="border-top:1px solid #e6ecf4;padding-top:12px;font-size:12px;color:#8892a2">{escape(result["notice"])}</p>'
        )
    lines.append("</div></body></html>")
    rendered = "\n".join(lines)
    if link_mode == "plain_address":
        # Source insights and titles may also contain literal web addresses.
        rendered = re.sub(r"https?://", "", rendered, flags=re.IGNORECASE)
    return rendered
