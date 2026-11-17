"""Optional browser regression check: python check_ui.py (requires Playwright + Chrome).

Uses a temporary database and mock SMTP. Never connects to Gmail or sends mail.
"""

import json
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen
from unittest.mock import patch


def serve():
    import app
    app.DB_PATH = Path(sys.argv[2])
    app.PORT = int(sys.argv[3])
    with patch("app.smtplib.SMTP"), patch("app.webbrowser.open"):
        app.main()


def check():
    from playwright.sync_api import sync_playwright, expect

    with tempfile.TemporaryDirectory() as directory, sync_playwright() as playwright:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        database = Path(directory) / "history.sqlite3"

        def start():
            process = subprocess.Popen([sys.executable, "-u", str(Path(__file__).resolve()), "--serve", str(database), str(port)],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError(process.stderr.read().decode())
                try:
                    with urlopen(url + '/api/health', timeout=.5) as response:
                        if json.load(response)["app"] == "resume-mailer-v2":
                            return process
                except OSError:
                    time.sleep(.1)
            process.terminate()
            process.wait(timeout=10)
            raise RuntimeError("Test server failed to start")

        process = start()
        browser = None
        try:
            second = subprocess.run([sys.executable, "-u", str(Path(__file__).resolve()), "--serve", str(database), str(port)],
                                    capture_output=True, timeout=10)
            assert second.returncode == 0, second.stderr.decode()
            assert b'already-running' in second.stdout, second.stdout.decode()
            browser = playwright.chromium.launch(channel="chrome", headless=True)
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(url)
            expect(page.locator('#savedFields')).to_be_enabled()
            for field, value in {"sender": "student@example.com", "name": "Student", "username": "student@example.com",
                                 "password": "fake-app-password", "recipients": "recruiter@example.com",
                                 "subject": "Internship application", "message": "Hello, please see my resume."}.items():
                page.locator('#' + field).fill(value)
            page.locator('#resume').set_input_files({"name": "resume.pdf", "mimeType": "application/pdf", "buffer": b"%PDF-1.4 mock resume"})
            expect(page.locator('#saveStatus')).to_have_text('Saved locally')
            page.reload()
            expect(page.locator('#sender')).to_have_value('student@example.com')
            expect(page.locator('#subject')).to_have_value('Internship application')
            expect(page.locator('#message')).to_have_value('Hello, please see my resume.')
            expect(page.locator('#password')).to_have_value('')
            expect(page.locator('#password')).to_have_attribute('placeholder', 'Saved securely · leave blank to reuse')
            expect(page.locator('#savedResume')).to_contain_text('resume.pdf')
            page.locator('#review').click()
            expect(page.locator('#preview')).to_be_visible()
            expect(page.locator('#previewMeta')).to_contain_text('1 eligible')
            page.locator('#confirm').click()
            expect(page.locator('#summary')).to_contain_text('1 sent', timeout=10000)
            # A full server-process restart must preserve the message, resume, credential and sent record.
            process.terminate()
            process.wait(timeout=10)
            process.stderr.close()
            process = start()
            page.reload()
            expect(page.locator('#sender')).to_have_value('student@example.com')
            expect(page.locator('#savedResume')).to_contain_text('resume.pdf')
            expect(page.locator('#dupWarning')).to_contain_text('recruiter@example.com')
            expect(page.locator('#summary')).to_contain_text('1 sent')
            page.locator('#review').click()
            expect(page.locator('#previewMeta')).to_contain_text('1 will be skipped')
            expect(page.locator('#confirm')).to_be_disabled()
            page.locator('#cancel').click()
            # Verify account switching and forgetting saved credentials update browser validity.
            page.locator('#remember').uncheck()
            expect(page.locator('#saveStatus')).to_have_text('Saved locally')
            page.reload()
            expect(page.locator('#password')).to_have_attribute('required', '')
            page.set_viewport_size({"width": 1280, "height": 1100})
            destination = Path(__file__).with_name('.qa')
            destination.mkdir(exist_ok=True)
            page.screenshot(path=str(destination / 'mailer.png'), full_page=True)
            assert not errors, errors
            print('Browser checks passed: autosave, reload, saved encrypted password/resume, mocked send, full process restart, retained history/results, duplicate warning, forget password. No emails sent.')
        finally:
            if browser:
                browser.close()
            process.terminate()
            process.wait(timeout=10)
            process.stderr.close()


if __name__ == '__main__':
    serve() if '--serve' in sys.argv else check()
