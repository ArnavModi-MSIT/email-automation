import base64
import json
import os
import sqlite3
import smtplib
import threading
import unittest
import tempfile
import time
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import app
from storage import Storage, protect_password


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
        app.STATE.update(running=True, error="", items=[dict(email=r, status="Queued", detail="") for r in config["recipients"]])

    def test_validation_and_deduplication(self):
        self.assertEqual(self.config()["recipients"], ["a@example.com", "b@example.com"])
        for overrides in [dict(recipients="bad"), dict(subject="x\r\nBcc: evil"), dict(attachment=""), dict(delay=0), dict(security="none")]:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.config(**overrides)

    @patch("app.smtplib.SMTP")
    def test_individual_messages_with_attachment(self, constructor):
        config = self.config()
        self.prepare(config)
        smtp = constructor.return_value
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
        constructor.return_value.login.side_effect = smtplib.SMTPAuthenticationError(535, b"Bad credentials")
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
        self.assertEqual(constructor.return_value.send_message.call_count, 1)
        self.assertEqual(len(app.recent_history()), 2)

    def test_expiry_at_30_days(self):
        with patch("storage.time.time", return_value=1000):
            app.record_sent("a@example.com")
        with patch("storage.time.time", return_value=1000 + app.COOLDOWN - 1):
            self.assertEqual(len(app.recent_history()), 1)
        with patch("storage.time.time", return_value=1000 + app.COOLDOWN):
            self.assertEqual(app.recent_history(), [])

    @patch("app.smtplib.SMTP")
    def test_failure_not_added_to_history(self, constructor):
        config = self.config(recipients="a@example.com")
        self.prepare(config)
        constructor.return_value.login.side_effect = smtplib.SMTPAuthenticationError(535, b"Bad credentials")
        app.send_queue(config)
        self.assertEqual(app.recent_history(), [])

    @patch("app.smtplib.SMTP")
    def test_quit_failure_does_not_lose_success(self, constructor):
        config = self.config(recipients="a@example.com")
        self.prepare(config)
        constructor.return_value.quit.side_effect = smtplib.SMTPServerDisconnected("Closed")
        app.send_queue(config)
        self.assertEqual(app.STATE["items"][0]["status"], "Sent")
        self.assertEqual(app.store().load_queue()["items"][0]["status"], "Sent")
        self.assertEqual(app.recent_history()[0]["status"], "Sent")

    @patch("app.smtplib.SMTP")
    def test_network_failure_during_send_is_protected(self, constructor):
        config = self.config(recipients="a@example.com")
        self.prepare(config)
        constructor.return_value.send_message.side_effect = TimeoutError("Timeout")
        app.send_queue(config)
        self.assertEqual(app.STATE["items"][0]["status"], "Uncertain")
        self.assertFalse(Storage(app.DB_PATH).reserve("A@example.com"))

    @patch("app.smtplib.SMTP")
    def test_history_write_failure_after_acceptance_stops_queue(self, constructor):
        config = self.config()
        self.prepare(config)
        with patch("app.record_sent", side_effect=sqlite3.OperationalError("Disk full")):
            app.send_queue(config)
        self.assertEqual([x["status"] for x in app.STATE["items"]], ["Uncertain", "Stopped"])
        self.assertFalse(app.store().reserve("a@example.com"))

    @patch("app.smtplib.SMTP")
    def test_unavailable_history_prevents_smtp(self, constructor):
        config = self.config()
        self.prepare(config)
        with patch.object(Storage, "reserve", side_effect=sqlite3.OperationalError("Read only")):
            app.send_queue(config)
        constructor.assert_not_called()
        self.assertIn("Queue stopped", app.STATE["error"])

    def test_restart_preserves_existing_database(self):
        with sqlite3.connect(app.DB_PATH) as db:
            db.execute("CREATE TABLE sent (email TEXT PRIMARY KEY, sent_at REAL NOT NULL)")
            db.execute("INSERT INTO sent VALUES ('a@example.com', ?)", (time.time(),))
        self.assertEqual(Storage(app.DB_PATH).history()[0]["email"], "a@example.com")
        self.assertFalse(Storage(app.DB_PATH).reserve("a@example.com"))

    def test_restart_does_not_resume_interrupted_queue(self):
        app.store().reserve("a@example.com")
        app.store().save_queue({"running": True, "error": "", "items": [
            {"email": "a@example.com", "status": "Sending", "detail": ""},
            {"email": "b@example.com", "status": "Queued", "detail": ""}]})
        state = Storage(app.DB_PATH).load_queue()
        self.assertFalse(state["running"])
        self.assertEqual([x["status"] for x in state["items"]], ["Uncertain", "Stopped"])
        self.assertFalse(Storage(app.DB_PATH).reserve("a@example.com"))

    def test_settings_and_resume_survive_new_storage_instance(self):
        data = dict(sender="me@example.com", username="me@example.com", host="smtp.gmail.com",
                    message="My draft", subject="Internship", remember_password=False)
        app.store().save_settings(data, b"resume contents", "resume.pdf")
        reopened = Storage(app.DB_PATH)
        self.assertEqual(reopened.settings()["message"], "My draft")
        self.assertEqual(reopened.saved_resume(), ("resume.pdf", b"resume contents"))
        reopened.save_settings({"subject": "Changed", "remember_password": False})
        self.assertEqual(reopened.saved_resume()[1], b"resume contents")

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_encrypted_password_reuse_and_forget(self):
        data = dict(sender="me@example.com", username="me@example.com", host="smtp.gmail.com", password="test-password-123")
        app.store().save_settings(data)
        self.assertNotIn(b"test-password-123", app.DB_PATH.read_bytes())
        saved = Storage(app.DB_PATH)
        self.assertNotIn("password", saved.settings())
        data["password"] = ""
        self.assertEqual(saved.saved_password(data), "test-password-123")
        saved.save_settings(data)
        self.assertEqual(saved.saved_password(data), "test-password-123")
        self.assertEqual(saved.saved_password({**data, "host": "other.example.com"}), "")
        saved.save_settings({**data, "remember_password": False})
        self.assertFalse(saved.settings()["has_password"])

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_switching_account_removes_previous_credential(self):
        data = dict(sender="me@example.com", username="me@example.com", host="smtp.gmail.com", password="secret")
        app.store().save_settings(data)
        app.store().save_settings({**data, "username": "other@example.com", "password": ""})
        self.assertFalse(app.store().settings()["has_password"])

    def test_concurrent_reservations_only_one_succeeds(self):
        # Initialize schema before threads to focus on the reservation transaction.
        app.store().history()
        results = []
        workers = [threading.Thread(target=lambda: results.append(Storage(app.DB_PATH).reserve("a@example.com"))) for _ in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual(sorted(results), [False, False, False, True])

    def test_backup_contains_committed_history(self):
        app.record_sent("a@example.com")
        app.store().backup()
        backup = next(app.DB_PATH.parent.glob("history-backup-*.sqlite3"))
        self.assertEqual(Storage(backup).history()[0]["email"], "a@example.com")

    def test_legacy_migration_preserves_and_merges_history(self):
        legacy = Path(self.directory.name) / 'legacy.sqlite3'
        Storage(legacy).record_sent('old@example.com')
        app.store().migrate_from(legacy)
        self.assertEqual(app.recent_history()[0]['email'], 'old@example.com')
        self.assertEqual(Storage(legacy).history()[0]['email'], 'old@example.com')
        app.record_sent('new@example.com')
        Storage(legacy).record_sent('another@example.com')
        app.store().migrate_from(legacy)
        self.assertEqual({row['email'] for row in app.recent_history()}, {'old@example.com', 'new@example.com', 'another@example.com'})


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        patcher = patch.object(app, "DB_PATH", Path(self.directory.name) / "history.sqlite3")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.server = app.LocalServer(("127.0.0.1", 0), app.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, path, data=None, token=app.TOKEN):
        request = Request(self.url + path, data=json.dumps(data).encode() if data is not None else None,
                          headers={"Content-Type": "application/json", "X-App-Token": token})
        with urlopen(request, timeout=3) as response:
            return json.load(response)

    def test_settings_api_and_saved_attachment(self):
        self.request('/api/settings', dict(sender="me@example.com", message="Saved draft", remember_password=False,
                                          attachment=base64.b64encode(b"my resume").decode(), filename="resume.pdf"))
        result = self.request('/api/settings')
        self.assertEqual(result["message"], "Saved draft")
        self.assertEqual(result["filename"], "resume.pdf")
        self.assertNotIn("attachment", result)
        self.assertNotIn("password", result)

    def test_unauthorized_request_cannot_save_or_read(self):
        for data in (None, {"message": "bad"}):
            with self.assertRaises(HTTPError) as error:
                self.request('/api/settings', data, token='wrong')
            self.assertIn(error.exception.code, (403, 404))

    def test_malformed_request_is_rejected(self):
        with self.assertRaises(HTTPError) as error:
            self.request('/api/settings', [])
        self.assertEqual(error.exception.code, 400)

    def test_second_launch_reuses_existing_app(self):
        with patch.object(app, 'PORT', self.server.server_port), patch('app.webbrowser.open') as browser, patch('app.initialize_storage') as initialize:
            app.main()
        browser.assert_called_once_with(self.url)
        initialize.assert_not_called()


if __name__ == "__main__":
    unittest.main()
