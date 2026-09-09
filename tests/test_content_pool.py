import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from urllib.error import HTTPError

MODULE = Path(__file__).resolve().parents[1] / "scripts/collect_content.py"
spec = importlib.util.spec_from_file_location("content_pool", MODULE)
pool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pool)

NOTE = "1234567890abcdef12345678"
ACCOUNT = "593032945e87e77791e03696"


def feed(body="正文" * 600, published="Tue, 08 Sep 2026 16:05:00 GMT", link=None):
    link = link or f"https://www.xiaohongshu.com/explore/{NOTE}"
    return f"""<rss version="2.0"><channel><title>测试账号</title><item>
        <title>测试笔记</title><link>{link}</link><guid>{link}</guid>
        <description><![CDATA[<p>{body}</p>]]></description>
        <pubDate>{published}</pubDate><author>作者</author>
        </item></channel></rss>""".encode()


class ContentPoolTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = pool.connect(Path(self.temp.name) / "pool.db")
        self.config = {"accounts": [ACCOUNT], "rsshub_url": "http://localhost:1200", "request_timeout_seconds": 2}

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def ingest(self, raw, source="source-a"):
        items = pool.parse_feed(raw, source)
        with self.db:
            pool.save_items(self.db, source, items, pool.now())

    def test_repeated_and_cross_source_entries_share_content(self):
        self.ingest(feed())
        self.ingest(feed(link=f"https://www.xiaohongshu.com/explore/{NOTE}?xsec_token=changed"))
        self.ingest(feed(), "source-b")
        self.assertEqual(self.db.execute("SELECT count(*) FROM items").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM observations").fetchone()[0], 2)
        self.assertEqual(len(self.db.execute("SELECT body_text FROM items").fetchone()[0]), 1200)

    def test_local_calendar_day_not_fetch_day(self):
        self.ingest(feed())
        self.assertEqual(len(pool.daily_items(self.db, "2026-09-09", "Asia/Shanghai")), 1)
        self.assertEqual(len(pool.daily_items(self.db, "2026-09-08", "America/New_York")), 1)
        self.assertEqual(len(pool.daily_items(self.db, "2026-09-08", "Asia/Shanghai")), 0)

    def test_degraded_feed_retained_but_not_assigned_a_date(self):
        self.ingest(feed(body="测试笔记", published="", link="https://cdn.example/cover.jpg"))
        item = self.db.execute("SELECT * FROM items").fetchone()
        self.assertIsNone(item["published_at"])
        self.assertIn("missing_note_id", json.loads(item["quality_flags"]))
        self.assertIn("possibly_incomplete_body", json.loads(item["quality_flags"]))
        self.assertEqual(pool.daily_items(self.db, "2026-09-09", "Asia/Shanghai"), [])

    def test_degraded_response_does_not_replace_known_content(self):
        self.ingest(feed())
        self.ingest(feed(body="测试笔记", published=""))
        item = self.db.execute("SELECT * FROM items").fetchone()
        self.assertEqual(len(item["body_text"]), 1200)
        self.assertIsNotNone(item["published_at"])

    def test_failure_empty_and_success_are_distinct(self):
        def fail(url, timeout):
            raise HTTPError(url, 503, "sensitive server error", {}, None)

        self.assertEqual(pool.collect(self.db, self.config, fail), 1)
        pool.collect(self.db, self.config, lambda *_: b"<rss><channel/></rss>")
        pool.collect(self.db, self.config, lambda *_: feed())
        rows = self.db.execute("SELECT status,error,raw_feed FROM fetch_runs ORDER BY id").fetchall()
        self.assertEqual([row["status"] for row in rows], ["failed", "empty", "success"])
        self.assertEqual(rows[0]["error"], "HTTP 503")
        self.assertEqual(rows[2]["raw_feed"], feed())

    def test_malformed_response_recorded_without_losing_items(self):
        self.ingest(feed())
        self.assertEqual(pool.collect(self.db, self.config, lambda *_: b"<html>login</html>"), 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM items").fetchone()[0], 1)

    def test_wechat_direct_feed_uses_configured_service(self):
        config = {"wechat_feeds": [{"id": "qbitai", "url": "http://127.0.0.1:8001/feed/123.xml"}],
                  "rsshub_url": "http://localhost:1200", "request_timeout_seconds": 2}
        seen = []
        def fetch(url, timeout):
            seen.append(url)
            return feed(link="https://mp.weixin.qq.com/s/test-article")
        self.assertEqual(pool.collect(self.db, config, fetch, platform="wechat"), 0)
        self.assertEqual(seen, ["http://127.0.0.1:8001/feed/123.xml"])
        self.assertEqual(self.db.execute("SELECT source_id FROM observations").fetchone()[0], "wechat:qbitai")

    def test_wechat_identity_ignores_tracking_parameters(self):
        first = "https://mp.weixin.qq.com/s?__biz=ABC%3D%3D&mid=123&idx=1&sn=old"
        second = "https://mp.weixin.qq.com/s?idx=1&mid=123&__biz=ABC%3D%3D&scene=21"
        self.assertEqual(pool.content_id_from_url(first, "wechat"), "article:ABC==:123:1")
        self.assertEqual(pool.content_id_from_url(first, "wechat"), pool.content_id_from_url(second, "wechat"))
        self.assertIsNone(pool.content_id_from_url("https://example.com/s/test", "wechat"))
        self.assertIsNone(pool.content_id_from_url("https://mp.weixin.qq.com/mp/verify", "wechat"))
        self.assertEqual(pool.content_id_from_url("https://mp.weixin.qq.com/s/abc_DEF-123?scene=21", "wechat"), "short:abc_DEF-123")

    def test_wechat_collection_and_deduplication(self):
        config = {"wechat_accounts": ["qbitai"], "rsshub_url": "http://localhost:1200", "request_timeout_seconds": 2}
        raw = feed(link="https://mp.weixin.qq.com/s/test-article")
        for _ in range(2):
            self.assertEqual(pool.collect(self.db, config, lambda *_: raw, platform="wechat"), 0)
        item = self.db.execute("SELECT * FROM items").fetchone()
        self.assertEqual(item["platform"], "wechat")
        self.assertEqual(json.loads(item["quality_flags"]), [])
        self.assertEqual(self.db.execute("SELECT count(*) FROM items").fetchone()[0], 1)
        with self.assertRaises(ValueError):
            pool.wechat_account_id("https://mp.weixin.qq.com/s/test")

    def test_profile_input(self):
        self.assertEqual(pool.account_id(f"https://www.xiaohongshu.com/user/profile/{ACCOUNT}?xsec_token=example"), ACCOUNT)
        for invalid in ["https://xhslink.com/example", "https://example.com/user/profile/" + ACCOUNT, "invalid"]:
            with self.assertRaises(ValueError):
                pool.account_id(invalid)

    def test_zhihu_content_types_and_tracking_parameters(self):
        for link in ["https://www.zhihu.com/question/45/answer/123", "https://www.zhihu.com/question/45/answer/123?utm_source=test"]:
            with self.db:
                pool.save_items(self.db, "zhihu:author:answers", pool.parse_feed(feed(link=link), "zhihu:author:answers", "zhihu"), pool.now())
        with self.db:
            pool.save_items(self.db, "zhihu:author:articles", pool.parse_feed(feed(link="https://zhuanlan.zhihu.com/p/123"), "zhihu:author:articles", "zhihu"), pool.now())
        rows = self.db.execute("SELECT platform,content_id,quality_flags FROM items ORDER BY content_id").fetchall()
        self.assertEqual([(r['platform'],r['content_id']) for r in rows], [('zhihu','answer:123'),('zhihu','article:123')])
        self.assertTrue(all(json.loads(r['quality_flags']) == [] for r in rows))

    def test_zhihu_profiles_and_selected_platform(self):
        self.assertEqual(pool.zhihu_account_id('https://www.zhihu.com/people/su-jian-lin-22'), 'su-jian-lin-22')
        with self.assertRaises(ValueError):
            pool.zhihu_account_id('https://example.com/people/su-jian-lin-22')
        config = dict(self.config, zhihu_accounts=['su-jian-lin-22'])
        urls = []
        def fetch(url, timeout):
            urls.append(url)
            return b'<rss><channel/></rss>'
        pool.collect(self.db, config, fetch, platform='zhihu')
        self.assertEqual(urls, ['http://localhost:1200/zhihu/people/answers/su-jian-lin-22', 'http://localhost:1200/zhihu/posts/people/su-jian-lin-22'])

    def test_twitter_identity_and_short_posts(self):
        for source, link in [('twitter:a','https://x.com/a/status/123'),('twitter:b','https://twitter.com/a/status/123?s=20')]:
            with self.db:
                pool.save_items(self.db, source, pool.parse_feed(feed(body='测试笔记',link=link),source,'twitter'),pool.now())
        row = self.db.execute('SELECT * FROM items').fetchone()
        self.assertEqual(row['content_id'],'tweet:123')
        self.assertEqual(json.loads(row['quality_flags']),[])
        self.assertEqual(self.db.execute('SELECT count(*) FROM items').fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM observations').fetchone()[0],2)
        self.assertIsNone(pool.content_id_from_url('https://x.com.evil.example/a/status/123','twitter'))

    def test_twitter_handles_and_options(self):
        for value in ['@OpenAI','https://x.com/OpenAI','https://twitter.com/OpenAI?lang=en']:
            self.assertEqual(pool.twitter_account_id(value),'openai')
        for value in ['https://x.com/SSI）','https://example.com/OpenAI','invalid-handle']:
            with self.assertRaises(ValueError):
                pool.twitter_account_id(value)
        config={'twitter_accounts':['openai'],'twitter':{'include_replies':True,'include_retweets':False}}
        self.assertEqual(list(pool.sources(config)),[('twitter','twitter:openai','/twitter/user/openai/includeReplies=1&includeRts=0')])

    def test_resume_retries_empty_and_unvisited_not_success(self):
        config=dict(self.config,twitter_accounts=['first','empty','new'])
        for source,status in [('twitter:first','success'),('twitter:empty','empty')]:
            self.db.execute('INSERT INTO fetch_runs(source_id,started_at,status) VALUES (?,?,?)',(source,pool.now(),status))
        self.db.commit()
        calls=[]
        def fetch(url, timeout):
            calls.append(url)
            return b'<rss><channel/></rss>'
        pool.collect(self.db,config,fetch,platform='twitter',resume=True)
        self.assertEqual(len(calls),2)
        self.assertTrue('/empty/' in calls[0] and '/new/' in calls[1])

    def test_interruption_is_recorded(self):
        def fetch(url,timeout):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            pool.collect(self.db,self.config,fetch)
        self.assertEqual(self.db.execute('SELECT status FROM fetch_runs').fetchone()[0],'interrupted')


if __name__ == "__main__":
    unittest.main()
