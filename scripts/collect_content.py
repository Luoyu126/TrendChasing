"""Poll RSSHub account feeds into a persistent, unfiltered content pool.

Run from any directory: python3 scripts/collect_content.py --help
Only PyYAML (already a project dependency) is required beyond the stdlib.
"""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, time as daytime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, parse_qs
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from trendradar.content_pool.store import open_database, ingest, scrub_responses
from trendradar.content_pool.runtime import run_lock
MAX_FEED_BYTES = 10 * 1024 * 1024
NOTE_PATH = re.compile(r"/(?:explore|discovery/item)/([0-9a-f]{24})(?:/|$)", re.I)


def now():
    return datetime.now(timezone.utc).isoformat()


def account_id(value):
    value = str(value).strip()
    if re.fullmatch(r"[0-9a-f]{24}", value, re.I):
        return value.lower()
    parsed = urlsplit(value)
    match = re.fullmatch(r"/user/profile/([0-9a-f]{24})/?", parsed.path, re.I)
    if parsed.scheme == "https" and parsed.hostname in {"www.xiaohongshu.com", "xiaohongshu.com"} and match:
        return match[1].lower()
    raise ValueError("小红书账号须为 24 位用户 ID 或完整 HTTPS 主页链接（不支持短链接）")


def zhihu_account_id(value):
    value = str(value).strip()
    if re.fullmatch(r"[A-Za-z0-9_-]+", value):
        return value
    parsed = urlsplit(value)
    match = re.fullmatch(r"/people/([A-Za-z0-9_-]+)/?", parsed.path)
    if parsed.scheme == "https" and parsed.hostname in {"www.zhihu.com", "zhihu.com"} and match:
        return match[1]
    raise ValueError("知乎账号须为主页标识或完整 HTTPS people 主页链接")


def twitter_account_id(value):
    value = str(value).strip()
    if value.startswith("@"):
        value = value[1:]
    if re.fullmatch(r"[A-Za-z0-9_]{1,15}", value):
        return value.lower()
    parsed = urlsplit(value)
    match = re.fullmatch(r"/([A-Za-z0-9_]{1,15})/?", parsed.path)
    if parsed.scheme == "https" and parsed.hostname in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"} and match:
        return match[1].lower()
    raise ValueError("Twitter 账号须为用户名、@用户名或完整 HTTPS 主页链接")


def wechat_account_id(value):
    value = str(value).strip()
    if re.fullmatch(r"[A-Za-z0-9_-]+", value):
        return value.lower()
    raise ValueError("微信公众号须填写微信号，不是文章链接")


def sources(config):
    if config.get("xiaohongshu", {}).get("enabled", True):
        for account in config.get("accounts", []):
            yield "xiaohongshu", "xiaohongshu:" + account, f"/xiaohongshu/user/{account}/notes"
    for account in config.get("zhihu_accounts", []):
        yield "zhihu", f"zhihu:{account}:answers", f"/zhihu/people/answers/{account}"
        yield "zhihu", f"zhihu:{account}:articles", f"/zhihu/posts/people/{account}"
    twitter = config.get("twitter", {})
    replies = int(twitter.get("include_replies", False))
    retweets = int(twitter.get("include_retweets", True))
    for account in config.get("twitter_accounts", []):
        yield "twitter", f"twitter:{account}", f"/twitter/user/{account}/includeReplies={replies}&includeRts={retweets}"

    if not config.get("wechat", {}).get("enabled", True):
        return
    for feed in config.get("wechat_feeds", []):
        yield "wechat", "wechat:" + feed["id"], feed["url"]
    for account in config.get("wechat_accounts", []):
        yield "wechat", f"wechat:{account}", f"/wechat/sogou/{account}"


def content_id_from_url(url, platform):
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        return None
    if platform == "xiaohongshu":
        match = NOTE_PATH.search(parsed.path)
        if parsed.hostname in {"www.xiaohongshu.com", "xiaohongshu.com"} and match:
            return match[1].lower()
    elif platform == "zhihu":
        if parsed.hostname in {"www.zhihu.com", "zhihu.com"}:
            match = re.fullmatch(r"/question/\d+/answer/(\d+)/?", parsed.path)
            if match:
                return "answer:" + match[1]
        if parsed.hostname == "zhuanlan.zhihu.com":
            match = re.fullmatch(r"/p/(\d+)/?", parsed.path)
            if match:
                return "article:" + match[1]
    elif platform == "wechat" and parsed.hostname == "mp.weixin.qq.com":
        query = parse_qs(parsed.query)
        if parsed.path.rstrip("/") == "/s":
            biz, mid, idx = (query.get(k, [""])[0] for k in ("__biz", "mid", "idx"))
            if biz and mid.isdigit() and idx.isdigit():
                return f"article:{biz}:{mid}:{idx}"
        match = re.fullmatch(r"/s/([A-Za-z0-9_~-]+)/?", parsed.path)
        if match:
            return "short:" + match[1]
    elif platform == "twitter":
        match = re.fullmatch(r"/(?:[A-Za-z0-9_]+/status|i/web/status)/(\d+)(?:/(?:photo|video)/\d+)?/?", parsed.path)
        if parsed.hostname in {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"} and match:
            return "tweet:" + match[1]
    return None


def load_config(path, pool_config=ROOT / "config/content_pool.yaml"):
    with Path(path).open(encoding="utf-8") as stream:
        config = yaml.safe_load(stream)
    ZoneInfo(config["timezone"])
    config["accounts"] = list(dict.fromkeys(account_id(v) for v in config.get("xiaohongshu", {}).get("accounts", [])))
    config["zhihu_accounts"] = list(dict.fromkeys(zhihu_account_id(v) for v in config.get("zhihu", {}).get("accounts", [])))
    config["twitter_accounts"] = list(dict.fromkeys(twitter_account_id(v) for v in config.get("twitter", {}).get("accounts", [])))
    config["wechat_accounts"] = list(dict.fromkeys(wechat_account_id(v) for v in config.get("wechat", {}).get("accounts", [])))
    config["wechat_feeds"] = config.get("wechat", {}).get("feeds", [])
    feed_ids = set()
    for feed in config["wechat_feeds"]:
        if not isinstance(feed, dict) or not re.fullmatch(r"[A-Za-z0-9_-]+", str(feed.get("id", ""))):
            raise ValueError("wechat.feeds 每项须包含有效 id 和 RSS url")
        url = urlsplit(feed.get("url", ""))
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.fragment:
            raise ValueError("wechat.feeds.url 须为无凭据、无片段的 HTTP(S) RSS 地址")
        if feed["id"] in feed_ids or feed["id"] in config["wechat_accounts"]:
            raise ValueError("微信公众号订阅 id 不能重复")
        for option in ("refresh_werss", "trust_published_at"):
            if option in feed and not isinstance(feed[option], bool):
                raise ValueError(f"wechat.feeds.{option} 必须为布尔值")
        feed_ids.add(feed["id"])
    for key in ("include_replies", "include_retweets"):
        if key in config.get("twitter", {}) and not isinstance(config["twitter"][key], bool):
            raise ValueError(f"twitter.{key} 必须是 YAML 布尔值 true 或 false")
    with Path(pool_config).open(encoding="utf-8") as stream:
        pool = yaml.safe_load(stream)
    database = (ROOT / pool["db_path"]).resolve()
    if "database" in config and (ROOT / config["database"]).resolve() != database:
        raise ValueError("source_database_must_match_content_pool")
    config["database"] = database
    config["rsshub_url"] = os.environ.get("RSSHUB_URL") or config["rsshub_url"]
    base = urlsplit(config["rsshub_url"])
    if base.scheme not in {"http", "https"} or not base.hostname or base.query or base.fragment or base.username:
        raise ValueError("rsshub_url 必须是无凭据、查询参数和片段的 HTTP(S) 服务地址")
    for key in ("poll_interval_minutes", "request_timeout_seconds"):
        config[key] = float(config[key])
        if not 0 < config[key] < 86400:
            raise ValueError(f"{key} 必须是大于零且小于 86400 的数")
    return config


def connect(path):
    return open_database(path)


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
    def handle_data(self, data):
        self.parts.append(data)


def plain_text(body):
    parser = TextExtractor()
    parser.feed(body)
    return " ".join(" ".join(parser.parts).split())


def parse_feed(raw, source_id, platform="xiaohongshu", trust_published_at=True):
    # RSSHub emits RSS 2.0 by default. Preserve raw XML alongside extracted fields.
    root = ET.fromstring(raw)
    if root.tag != "rss" or root.find("channel") is None:
        raise ValueError("服务未返回 RSS 2.0 文档")
    items = []
    for entry in root.findall("./channel/item"):
        fields = {child.tag.rsplit("}", 1)[-1]: child.text or "" for child in entry}
        link, guid = fields.get("link", "").strip(), fields.get("guid", "").strip()
        title = fields.get("title", "")
        body = fields.get("encoded") or fields.get("description", "")
        text = plain_text(body)
        flags = []
        note_id = content_id_from_url(link, platform) or content_id_from_url(guid, platform)
        if note_id is None:
            flags.append("missing_note_id" if platform == "xiaohongshu" else "missing_content_id")
            note_id = "provisional:" + hashlib.sha256((source_id + "\n" + (guid or link or title or body)).encode()).hexdigest()
        if content_id_from_url(link, platform) is None:
            flags.append("missing_note_link" if platform == "xiaohongshu" else "missing_content_link")
        published = None
        try:
            stamp = parsedate_to_datetime(fields.get("pubDate", ""))
            if stamp.tzinfo is not None:
                published = stamp.astimezone(timezone.utc).isoformat()
        except (ValueError, TypeError, OverflowError):
            pass
        if not trust_published_at:
            published = None
        if published is None:
            flags.append("missing_published_at")
        if not text or (platform != "twitter" and text == plain_text(title)):
            flags.append("possibly_incomplete_body")
        items.append({
            "platform": platform, "content_id": note_id, "url": link,
            "title": title, "author": fields.get("author") or fields.get("creator", ""),
            "body_html": body, "body_text": text, "published_at": published,
            "quality_flags": json.dumps(flags), "raw_entry": ET.tostring(entry, encoding="unicode"),
        })
    return items


def save_items(db, source_id, items, stamp):
    ingest(db, source_id, items, stamp)


def download(url, timeout):
    for attempt in range(3):
        try:
            request = Request(url, headers={"User-Agent": "TrendRadar-ContentPool/1.0", "Accept": "application/rss+xml"})
            with urlopen(request, timeout=timeout) as response:
                raw = response.read(MAX_FEED_BYTES + 1)
            if len(raw) > MAX_FEED_BYTES:
                raise ValueError("RSS response exceeds 10 MiB")
            return raw
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            retryable = not isinstance(exc, HTTPError) or exc.code == 429 or exc.code >= 500
            if isinstance(exc, HTTPError):
                exc.close()
            if attempt == 2 or not retryable:
                raise
            time.sleep(2 ** attempt)


def collect(db, config, fetch=download, platform=None, resume=False):
    failures = 0
    previous_runs = {r["source_id"]: r["started_at"] for r in db.execute(
        "SELECT source_id,max(started_at) AS started_at FROM fetch_runs GROUP BY source_id")}
    # After runner timeout, start with least recently attempted sources next time.
    ordered = list(sources(config)) if resume else sorted(sources(config), key=lambda s: previous_runs.get(s[1], ""))
    for source_platform, source_id, route in ordered:
        if platform and source_platform != platform:
            continue
        if resume:
            previous = db.execute("SELECT status FROM fetch_runs WHERE source_id=? ORDER BY id DESC LIMIT 1", (source_id,)).fetchone()
            if previous and previous["status"] in {"success", "partial"}:
                continue
        started = now()
        with db:
            run = db.execute("INSERT INTO fetch_runs(source_id,started_at,status) VALUES (?,?,?)",
                             (source_id, started, "running")).lastrowid
        raw = None
        try:
            url = route if urlsplit(route).scheme in {"http", "https"} else config["rsshub_url"].rstrip("/") + route
            feed_config = next((f for f in config.get("wechat_feeds", []) if source_id == "wechat:" + f["id"]), {}) if source_platform == "wechat" else {}
            if feed_config.get("external_werss", False):
                base = os.environ.get("WERSS_BASE_URL", "").rstrip("/")
                if not base:
                    raise ValueError("external_werss_not_configured")
                parsed = urlsplit(base)
                if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.query or parsed.fragment:
                    raise ValueError("external_werss_https_base_required")
                url = base + urlsplit(route).path
            if feed_config.get("refresh_werss", False):
                from scripts.werss_source import refresh_feed
                refresh_feed(feed_config, ROOT / config["wechat"]["credentials_file"], config["request_timeout_seconds"])
            raw = fetch(url, config["request_timeout_seconds"])
            items = parse_feed(raw, source_id, source_platform, trust_published_at=feed_config.get("trust_published_at", True))
            for item in items:
                if not item["author"] and feed_config.get("name"):
                    item["author"] = feed_config["name"]
            flagged = sum(bool(json.loads(item["quality_flags"])) for item in items)
            status = "empty" if not items else "partial" if flagged else "success"
            with db:
                save_items(db, source_id, items, started)
                db.executemany("INSERT OR IGNORE INTO fetch_run_items VALUES (?,?,?)", [(run,item['platform'],item['content_id']) for item in items])
                db.execute("""UPDATE fetch_runs SET finished_at=?,status=?,item_count=?,
                    flagged_count=?,raw_feed=? WHERE id=?""", (now(), status, len(items), flagged, raw, run))
                scrub_responses(db)
            print(f"{source_id}: {status}, returned={len(items)}, flagged={flagged}", flush=True)
        except KeyboardInterrupt:
            with db:
                db.execute("UPDATE fetch_runs SET finished_at=?,status='interrupted' WHERE id=?", (now(), run))
            raise
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, ET.ParseError) as exc:
            failures += 1
            # Do not copy server error pages, request tokens, or cookies into logs.
            error = f"HTTP {exc.code}" if isinstance(exc, HTTPError) else (str(exc) if str(exc) in {"external_werss_not_configured", "external_werss_https_base_required"} else type(exc).__name__)
            if isinstance(exc, HTTPError):
                exc.close()
            with db:
                db.execute("UPDATE fetch_runs SET finished_at=?,status='failed',error=?,raw_feed=? WHERE id=?",
                           (now(), error, raw, run))
            print(f"{source_id}: failed ({error})", file=sys.stderr)
    return failures


def daily_items(db, day, zone):
    day = date.fromisoformat(day)
    zone = ZoneInfo(zone)
    start = datetime.combine(day, daytime.min, zone).astimezone(timezone.utc).isoformat()
    end = datetime.combine(day + timedelta(days=1), daytime.min, zone).astimezone(timezone.utc).isoformat()
    return [dict(row) for row in db.execute(
        "SELECT * FROM items WHERE published_at>=? AND published_at<? ORDER BY published_at,content_id", (start, end))]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config/content_sources.yaml")
    parser.add_argument("--pool-config", type=Path, default=ROOT / "config/content_pool.yaml")
    commands = parser.add_subparsers(dest="command", required=True)
    poll = commands.add_parser("collect", help="抓取全部配置账号；不做分类或推送")
    poll.add_argument("--watch", action="store_true", help="按配置间隔持续轮询，Ctrl-C 停止")
    poll.add_argument("--platform", choices=["xiaohongshu", "zhihu", "twitter", "wechat"], help="仅采集指定平台；默认全部")
    poll.add_argument("--resume", action="store_true", help="仅重试最近未成功返回内容的来源；不能与 --watch 同用")
    listing = commands.add_parser("list", help="按配置时区查询发布日期，输出 JSON")
    listing.add_argument("--date", required=True, help="YYYY-MM-DD")
    commands.add_parser("status", help="最近 20 次采集记录及内容池统计")
    args = parser.parse_args()
    if args.command == "collect" and args.resume and args.watch:
        parser.error("--resume 仅用于补采，请不要与 --watch 同用")
    config = load_config(args.config, args.pool_config)
    if args.command == "collect" and not any(not args.platform or p == args.platform for p, _, _ in sources(config)):
        parser.error("所选平台尚未配置账号；请填写对应的 accounts 列表")
    db = connect(config["database"])
    try:
        if args.command == "list":
            print(json.dumps(daily_items(db, args.date, config["timezone"]), ensure_ascii=False, indent=2))
        elif args.command == "status":
            result = {
                "total_items": db.execute("SELECT count(*) FROM items").fetchone()[0],
                "undated_items": db.execute("SELECT count(*) FROM items WHERE published_at IS NULL").fetchone()[0],
                "recent_runs": [dict(row) for row in db.execute("SELECT id,source_id,started_at,finished_at,status,item_count,flagged_count,error FROM fetch_runs ORDER BY id DESC LIMIT 20")],
            }
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            while True:
                with run_lock(config["database"].with_suffix(".run.lock")):
                    failed = collect(db, config, platform=args.platform, resume=args.resume)
                if not args.watch:
                    return 1 if failed else 0
                time.sleep(config["poll_interval_minutes"] * 60)
    except KeyboardInterrupt:
        return 130
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
