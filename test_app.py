import base64
import smtplib
import unittest
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import app


class MailerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        patcher = patch.object(app, "DB_PATH", Path(self.directory.name) / "history.sqlite3")
        patcher.start()
        self.addCleanup(patcher.stop)

    def config(self, **overrides):
        data = dict(sender="me@example.com", name="Student", username="me@example.com",
                    password="secret", host="smtp.example.com", port=587, delay=1,
                    security="starttls", recipients="a@example.com, b@example.com; a@example.com",
                    subject="Application", message="Please see my resume.", filename="resume.pdf",
                    attachment=base64.b64encode(b"test resume").decode())
        data.update(overrides)
        return app.validate(data)

    def prepare(self, config):
        app.STOP.clear()
        app.STATE.update(running=True, items=[dict(email=r, status="Queued", detail="") for r in config["recipients"]])

    def test_validation_and_deduplication(self):
        self.assertEqual(self.config()["recipients"], ["a@example.com", "b@example.com"])
        for overrides in [dict(recipients="bad"), dict(subject="x\r\nBcc: evil"), dict(attachment=""), dict(delay=0), dict(security="none")]:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.config(**overrides)

    @patch("app.smtplib.SMTP")
    def test_individual_messages_with_attachment(self, constructor):
        config = self.config()
        self.prepare(config)
        smtp = constructor.return_value.__enter__.return_value
        with patch.object(app.STOP, "wait", return_value=False):
            app.send_queue(config)
        messages = [call.args[0] for call in smtp.send_message.call_args_list]
        self.assertEqual([m["To"] for m in messages], ["a@example.com", "b@example.com"])
        self.assertTrue(all(m["Cc"] is None and m["Bcc"] is None for m in messages))
        self.assertEqual(next(messages[0].iter_attachments()).get_payload(decode=True), b"test resume")
        self.assertEqual([x["status"] for x in app.STATE["items"]], ["Sent", "Sent"])
        self.assertFalse(app.STATE["running"])
        self.assertEqual(config["password"], "")
        self.assertEqual(smtp.starttls.call_count, 2)

    @patch("app.smtplib.SMTP")
    def test_authentication_failure_stops_remaining(self, constructor):
        config = self.config()
        self.prepare(config)
        constructor.return_value.__enter__.return_value.login.side_effect = smtplib.SMTPAuthenticationError(535, b"Bad credentials")
        app.send_queue(config)
        self.assertEqual([x["status"] for x in app.STATE["items"]], ["Failed", "Stopped"])

    @patch("app.smtplib.SMTP")
    def test_stop_prevents_sending(self, constructor):
        config = self.config()
        self.prepare(config)
        app.STOP.set()
        app.send_queue(config)
        constructor.assert_not_called()
        self.assertTrue(all(x["status"] == "Stopped" for x in app.STATE["items"]))

    @patch("app.smtplib.SMTP")
    def test_recent_recipient_skipped_across_queues(self, constructor):
        app.record_sent("A@example.com")
        config = self.config()
        self.prepare(config)
        app.send_queue(config)
        self.assertEqual([x["status"] for x in app.STATE["items"]], ["Skipped", "Sent"])
        self.assertEqual(constructor.return_value.__enter__.return_value.send_message.call_count, 1)
        self.assertEqual(len(app.recent_history()), 2)

    def test_expiry_at_30_days(self):
        with patch("app.time.time", return_value=1000):
            app.record_sent("a@example.com")
        with patch("app.time.time", return_value=1000 + app.COOLDOWN - 1):
            self.assertEqual(len(app.recent_history()), 1)
        with patch("app.time.time", return_value=1000 + app.COOLDOWN):
            self.assertEqual(app.recent_history(), [])

    @patch("app.smtplib.SMTP")
    def test_failure_not_added_to_history(self, constructor):
        config = self.config(recipients="a@example.com")
        self.prepare(config)
        constructor.return_value.__enter__.return_value.login.side_effect = smtplib.SMTPAuthenticationError(535, b"Bad credentials")
        app.send_queue(config)
        self.assertEqual(app.recent_history(), [])


if __name__ == "__main__":
    unittest.main()
