import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import test_pool_workflow as workflow
from test_pool_workflow import item

from trendradar.content_pool.delivery import deliver, smtp_send
from trendradar.content_pool.runtime import load_env, run_lock


class DeliveryTests(unittest.TestCase):
    setUp = workflow.Workflow.setUp
    tearDown = workflow.Workflow.tearDown
    count = workflow.Workflow.count
    ingest = workflow.Workflow.ingest

    def ready(self):
        self.ingest(item())
        self.processor.process()
        b = self.digests.prepare(notice="采集暂缺：X 1 个订阅；后续补抓。")
        self.digests.preview(b)
        return b

    @patch.dict(
        os.environ,
        {
            "EMAIL_TO": "a@example.test,b@example.test",
            "EMAIL_FROM": "sender@example.test",
            "EMAIL_PASSWORD": "fake",
            "EMAIL_SMTP_SERVER": "smtp.example.test",
        },
    )
    def test_partial_smtp_retry_only_failed_recipient(self):
        b = self.ready()
        calls = []

        def send(to, *args):
            calls.append(to)
            return (
                ("success", "receipt") if to.startswith("a") else ("failed", "refused")
            )

        result = deliver(self.digests, b, send)
        self.assertEqual(result["status"], "delivery_pending")
        self.assertEqual(self.count("items"), 1)
        result = deliver(
            self.digests,
            b,
            lambda to, *args: calls.append(to) or ("success", "receipt"),
        )
        self.assertEqual(calls, ["a@example.test", "b@example.test", "b@example.test"])
        self.assertEqual(result["status"], "sent")
        self.assertEqual(self.count("items"), 0)

    @patch.dict(
        os.environ,
        {
            "EMAIL_TO": "a@example.test",
            "EMAIL_FROM": "sender@example.test",
            "EMAIL_PASSWORD": "fake",
            "EMAIL_SMTP_SERVER": "smtp.example.test",
        },
    )
    def test_unknown_send_outcome_blocks_automatic_resend(self):
        b = self.ready()
        with patch("trendradar.content_pool.delivery.smtp_send"):
            result = deliver(self.digests, b, lambda *_: ("uncertain", "disconnect"))
        retry = MagicMock()
        deliver(self.digests, b, retry)
        retry.assert_not_called()
        self.assertEqual(result["uncertain"], 1)
        self.assertEqual(self.count("items"), 1)

    def test_notice_small_and_html_safe(self):
        b = self.ready()
        html = (self.root / "reports" / (b + ".html")).read_text()
        self.assertEqual(html.count("采集暂缺"), 1)
        self.assertIn("font-size:12px", html)
        self.assertNotIn("原始完整摘要", html)

    @patch.dict(os.environ, {
        "EMAIL_TO": "a@example.test", "EMAIL_FROM": "sender@example.test",
        "EMAIL_PASSWORD": "fake", "EMAIL_SMTP_SERVER": "smtp.example.test",
        "EMAIL_SMTP_PORT": "465",
    })
    def test_plain_address_delivery_preserves_report_and_both_mime_parts(self):
        b = self.ready()
        report = self.root / "reports" / (b + ".html")
        original = report.read_bytes()
        self.digests.meter.config["email_link_mode"] = "plain_address"
        server = MagicMock()
        server.send_message.return_value = {}
        with patch("smtplib.SMTP_SSL", return_value=server):
            result = deliver(self.digests, b, smtp_send, cleanup=False)
        self.assertEqual(result["status"], "sent")
        msg = server.send_message.call_args.args[0]
        for kind in ("plain", "html"):
            body = msg.get_body(preferencelist=(kind,)).get_content()
            self.assertIn("example.org/1", body)
            self.assertIn("Main point", body)
            self.assertNotRegex(body, r"(?i)https?://|href=")
        self.assertIn("<h1", msg.get_body(preferencelist=("html",)).get_content())
        self.assertEqual(report.read_bytes(), original)
        self.assertIn(b'https://example.org/1', original)


class RuntimeTests(unittest.TestCase):
    def test_literal_env_not_executed_and_existing_env_wins(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"EXISTING": "kept"}, clear=True),
        ):
            path = Path(tmp) / "secrets.env"
            path.write_text('EXISTING=replaced\nNEW="$(do-not-execute)"\n')
            load_env(path)
            self.assertEqual(os.environ["EXISTING"], "kept")
            self.assertEqual(os.environ["NEW"], "$(do-not-execute)")

    def test_lock_blocks_overlap(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            run_lock(Path(tmp) / "lock"),
            self.assertRaises(RuntimeError),
            run_lock(Path(tmp) / "lock"),
        ):
            pass

    @patch.dict(
        os.environ,
        {
            "EMAIL_FROM": "a@example.test",
            "EMAIL_SMTP_SERVER": "smtp.example.test",
            "EMAIL_PASSWORD": "fake",
        },
    )
    def test_quit_failure_after_acceptance_is_success(self):
        server = MagicMock()
        server.send_message.return_value = {}
        server.quit.side_effect = OSError()
        with patch("smtplib.SMTP_SSL", return_value=server):
            state, _ = smtp_send(
                "b@example.test", "subject", "<p>test</p>", "<id@example.test>"
            )
        self.assertEqual(state, "success")

    @patch.dict(os.environ, {
        "EMAIL_FROM": "a@example.test", "EMAIL_SMTP_SERVER": "smtp.example.test",
        "EMAIL_PASSWORD": "fake", "EMAIL_SMTP_PORT": "465",
    })
    def test_smtp_wire_headers_body_and_queue_receipt(self):
        import json
        from email import policy
        from email.parser import BytesParser
        server = MagicMock()
        server.data.return_value = (250, b"ok queue id test-123")
        captured = []

        def submit(msg, **kwargs):
            wire = msg.as_bytes()
            captured.append(BytesParser(policy=policy.default).parsebytes(wire))
            self.assertNotIn(b"\n", wire.replace(b"\r\n", b""))
            server.data(wire)
            return {}

        server.send_message.side_effect = submit
        with patch("smtplib.SMTP_SSL", return_value=server):
            state, receipt = smtp_send("b@example.test", "测试", '<p>实际观点 <a href="https://example.test/post">原文</a></p>', '<test@example.test>')
        self.assertEqual(state, "success")
        self.assertEqual(json.loads(receipt)["smtp_data"]["reply"], "ok queue id test-123")
        msg = captured[0]
        self.assertIsNotNone(msg["Date"])
        self.assertIn("实际观点", msg.get_body(preferencelist=("plain",)).get_content())
        self.assertIn("https://example.test/post", msg.get_body(preferencelist=("plain",)).get_content())
        self.assertEqual(msg.get_body(preferencelist=("html",))["Content-Transfer-Encoding"], "quoted-printable")
