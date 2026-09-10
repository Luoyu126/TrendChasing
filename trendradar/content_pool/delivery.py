"""Per-recipient SMTP acceptance ledger. Ambiguous sends never auto-retry."""

import os
import json
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate
from email.policy import SMTP
from html.parser import HTMLParser


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {"p", "div", "h1", "h2", "h3", "li", "br"}:
            self.parts.append("\n")
        if tag == "a":
            url = dict(attrs).get("href", "")
            if url.startswith(("https://", "http://")):
                self.parts.append(url + " ")

    def handle_data(self, data):
        self.parts.append(data)


def plain_text(html):
    parser = _PlainText()
    parser.feed(html)
    return "\n".join(line.strip() for line in "".join(parser.parts).splitlines() if line.strip())


def recipients():
    values = list(
        dict.fromkeys(
            v.strip() for v in os.environ.get("EMAIL_TO", "").split(",") if v.strip()
        )
    )
    if not values or any("@" not in v or "\n" in v or "\r" in v for v in values):
        raise ValueError("email_recipients_missing_or_invalid")
    return values


def smtp_send(recipient, subject, html, message_id):
    """Success means SMTP DATA accepted, not proof of arrival in the inbox."""
    sender = os.environ["EMAIL_FROM"]
    host = os.environ["EMAIL_SMTP_SERVER"]
    port = int(os.environ.get("EMAIL_SMTP_PORT", "465"))
    msg = EmailMessage(policy=SMTP)
    msg["From"] = formataddr(("TrendRadar 日报", sender))
    msg["To"] = recipient
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    msg["Date"] = formatdate(localtime=True)
    msg.set_content(plain_text(html), cte="quoted-printable")
    msg.add_alternative(html, subtype="html", cte="quoted-printable")
    server = None
    submitting = False
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(
                host, port, timeout=45, context=ssl.create_default_context()
            )
        else:
            server = smtplib.SMTP(host, port, timeout=45)
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
        server.login(sender, os.environ["EMAIL_PASSWORD"])
        data_receipt = {}
        original_data = server.data

        def traced_data(message):
            code, reply = original_data(message)
            data_receipt.update(code=code, reply=reply.decode(errors="replace"))
            return code, reply

        server.data = traced_data
        submitting = True
        refused = server.send_message(msg, from_addr=sender, to_addrs=[recipient])
        if refused:
            return "failed", "recipient_refused"
        return "success", json.dumps({"message_id": message_id, "smtp_data": data_receipt})
    except (
        smtplib.SMTPRecipientsRefused,
        smtplib.SMTPSenderRefused,
        smtplib.SMTPDataError,
    ):
        return "failed", "smtp_rejected"
    except Exception as exc:  # noqa: BLE001 - classify transport outcome without leaking credentials
        return ("uncertain" if submitting else "failed"), type(exc).__name__
    finally:
        if server:
            # QUIT failure cannot undo an already accepted DATA response.
            try:
                server.quit()
            except Exception:  # noqa: BLE001 - best effort connection teardown
                server.close()


def deliver(digests, batch_id, send=smtp_send, cleanup=True):
    batch = digests.get(batch_id)
    if batch["status"] == "cleaned":
        return {"status": "already_cleaned", "batch_id": batch_id}
    mode = digests.meter.config.get("email_link_mode", "full")
    if mode not in ("full", "plain_address"):
        raise ValueError("invalid_email_link_mode")
    if mode == "plain_address":
        from .presentation import render

        report = json.loads((digests.report_dir / (batch_id + ".json")).read_text())
        body = render(report, digests.meter.config.get("timezone", "Asia/Shanghai"), mode)
    else:
        body = (digests.report_dir / (batch_id + ".html")).read_text()
    from .store import now

    db = digests.store.db
    rows = list(db.execute("SELECT * FROM deliveries WHERE batch_id=?", (batch_id,)))
    if not rows:
        for key in ("EMAIL_FROM", "EMAIL_SMTP_SERVER", "EMAIL_PASSWORD"):
            if not os.environ.get(key):
                raise ValueError("missing_" + key)
        digests.set_targets(batch_id, {"email:" + r: 1 for r in recipients()})
        rows = list(
            db.execute("SELECT * FROM deliveries WHERE batch_id=?", (batch_id,))
        )
    from datetime import datetime
    from zoneinfo import ZoneInfo

    day = (
        datetime.fromisoformat(batch["created_at"])
        .astimezone(ZoneInfo(digests.meter.config.get("timezone", "Asia/Shanghai")))
        .strftime("%Y-%m-%d")
    )
    window = digests.store.cache_get("window:" + batch_id)
    if window:
        day = window["date"]
    for row in rows:
        target = row["target"]
        if not target.startswith("email:"):
            continue
        if row["state"] == "sending":
            digests.acknowledge(
                batch_id,
                target,
                row["part"],
                "uncertain",
                "interrupted_after_send_started",
            )
            continue
        if row["state"] in ("success", "uncertain"):
            continue
        # Commit the claim before SMTP. A lost result leaves sending/uncertain.
        with db:
            claimed = db.execute(
                "UPDATE deliveries SET state='sending',updated_at=? WHERE batch_id=? AND target=? AND part=? AND state IN ('pending','failed')",
                (now(), batch_id, target, row["part"]),
            ).rowcount
        if claimed != 1:
            continue
        from .store import digest

        domain = os.environ["EMAIL_FROM"].rsplit("@", 1)[-1]
        message_id = f"<{batch_id}.{digest(target)[:16]}@{domain}>"
        try:
            state, receipt = send(
                target.removeprefix("email:"), f"AI 日报 · {day}", body, message_id
            )
        except Exception:  # noqa: BLE001 - unknown outcome must block cleanup
            state, receipt = "uncertain", "sender_interrupted"
        digests.acknowledge(batch_id, target, row["part"], state, receipt)
    states = [
        r[0]
        for r in db.execute(
            "SELECT state FROM deliveries WHERE batch_id=?", (batch_id,)
        )
    ]
    success = bool(states) and all(s == "success" for s in states)
    if success and cleanup:
        digests.cleanup(batch_id, execute=True)
    return {
        "batch_id": batch_id,
        "status": "sent" if success else "delivery_pending",
        "targets": len(states),
        "success": states.count("success"),
        "uncertain": states.count("uncertain"),
    }
