import base64
import json
import mimetypes
import os
import re
import secrets
import socket
import smtplib
import ssl
import sqlite3
import threading
import webbrowser
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from storage import Storage, COOLDOWN

TOKEN = secrets.token_urlsafe(32)
LOCK = threading.Lock()
STOP = threading.Event()
STATE = {"running": False, "items": [], "error": ""}
EMAIL = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}$")
MAX_BODY = 20 * 1024 * 1024
LEGACY_DB_PATH = Path(__file__).resolve().with_name("send_history.sqlite3")
DEFAULT_DB_PATH = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "share"))) / "ResumeMailer" / "send_history.sqlite3"
DB_PATH = DEFAULT_DB_PATH
PORT = 8765
APP_ID = "resume-mailer-v2"


class LocalServer(ThreadingHTTPServer):
    # Windows SO_REUSEADDR permits two listeners on the same port. Exclusive
    # binding ensures a second launch reopens the existing process instead.
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def store():
    return Storage(DB_PATH)


def initialize_storage():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH == DEFAULT_DB_PATH:
        try:
            store().migrate_from(LEGACY_DB_PATH)
        except sqlite3.Error as exc:
            print(f"Skipped importing legacy history: {exc}", flush=True)
    store().backup()
    recent_history()


def recent_history():
    return store().history()


def record_sent(recipient):
    store().record_sent(recipient)


def persist_state():
    # Caller holds LOCK. A failed write must stop the queue before further sends.
    store().save_queue(STATE)


def attachment_from(data):
    if data.get("attachment"):
        attachment = base64.b64decode(data["attachment"], validate=True)
        filename = Path(data.get("filename", "resume.pdf")).name
    else:
        filename, attachment = store().saved_resume()
    if not attachment or len(attachment) > 10 * 1024 * 1024:
        raise ValueError("Attach a resume no larger than 10 MB.")
    if not filename or any(c in filename for c in "\r\n"):
        raise ValueError("Invalid attachment filename.")
    return filename, attachment


def validate(data):
    if not isinstance(data, dict):
        raise ValueError("Expected an object.")
    data = dict(data)
    if not data.get("password"):
        data["password"] = store().saved_password(data)
    recipients = list(dict.fromkeys(x.strip().lower() for x in re.split(r"[,;\s]+", data.get("recipients", "")) if x.strip()))
    if not recipients or len(recipients) > 500:
        raise ValueError("Enter between 1 and 500 recipients.")
    for address in recipients + [data.get("sender", "")]:
        if not EMAIL.fullmatch(address):
            raise ValueError(f"Invalid email address: {address}")
    for key in ("subject", "host", "username", "password", "message"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError(f"{key.capitalize()} is required.")
    if any(c in data["subject"] + data.get("name", "") for c in "\r\n"):
        raise ValueError("Subject and sender name must be single lines.")
    port = int(data.get("port", 587))
    delay = float(data.get("delay", 10))
    if not 1 <= port <= 65535 or not 1 <= delay <= 3600:
        raise ValueError("Use a valid port and a delay between 1 and 3600 seconds.")
    if data.get("security") not in ("starttls", "ssl"):
        raise ValueError("Choose STARTTLS or SSL/TLS.")
    filename, attachment = attachment_from(data)
    return {**data, "recipients": recipients, "port": port, "delay": delay, "attachment": attachment, "filename": filename}


def send_queue(config):
    try:
        for index, recipient in enumerate(config["recipients"]):
            if STOP.is_set():
                break
            if not store().reserve(recipient):
                with LOCK:
                    STATE["items"][index].update(status="Skipped", detail="Already emailed or has an uncertain attempt within the last 30 days.")
                    persist_state()
                continue
            with LOCK:
                STATE["items"][index]["status"] = "Sending"
                persist_state()
            accepted = False
            transmitting = False
            smtp = None
            try:
                message = EmailMessage()
                from email.utils import formataddr, make_msgid, formatdate
                message["From"] = formataddr((config.get("name", ""), config["sender"]))
                message["To"] = recipient
                message["Subject"] = config["subject"]
                message["Date"] = formatdate(localtime=True)
                message["Message-ID"] = make_msgid()
                message.set_content(config["message"])
                mime = mimetypes.guess_type(config["filename"])[0] or "application/octet-stream"
                message.add_attachment(config["attachment"], maintype=mime.split("/")[0], subtype=mime.split("/")[1], filename=config["filename"])
                context = ssl.create_default_context()
                smtp = (smtplib.SMTP_SSL(config["host"], config["port"], timeout=30, context=context)
                        if config["security"] == "ssl" else smtplib.SMTP(config["host"], config["port"], timeout=30))
                if config["security"] == "starttls":
                    smtp.ehlo()
                    smtp.starttls(context=context)
                    smtp.ehlo()
                smtp.login(config["username"], config["password"])
                transmitting = True
                smtp.send_message(message)
                accepted = True
                record_sent(recipient)
                status, detail = "Sent", "Accepted by the SMTP server; delivery is not guaranteed."
            except Exception as exc:
                uncertain = accepted or (transmitting and isinstance(exc, (OSError, smtplib.SMTPServerDisconnected)))
                if uncertain:
                    status = "Uncertain"
                    detail = "Delivery/history confirmation interrupted. Check Gmail Sent. Recipient is protected from resending."
                    STOP.set()
                else:
                    store().release(recipient)
                    status, detail = "Failed", str(exc)
                if isinstance(exc, smtplib.SMTPAuthenticationError):
                    STOP.set()
            finally:
                if smtp is not None:
                    try:
                        smtp.quit()
                    except Exception:
                        # QUIT failure cannot undo SMTP's earlier acceptance.
                        try:
                            smtp.close()
                        except Exception:
                            pass
            with LOCK:
                STATE["items"][index].update(status=status, detail=detail)
                persist_state()
            if index < len(config["recipients"]) - 1 and STOP.wait(config["delay"]):
                break
    except (sqlite3.Error, OSError):
        STOP.set()
        with LOCK:
            STATE["error"] = "Cannot save/read local history. Queue stopped to prevent duplicate emails."
            for item in STATE["items"]:
                if item["status"] == "Sending":
                    item.update(status="Uncertain", detail="Check Gmail Sent before retrying.")
    finally:
        config["password"] = ""
        with LOCK:
            for item in STATE["items"]:
                if item["status"] == "Queued":
                    item["status"] = "Stopped"
            STATE["running"] = False
            try:
                persist_state()
                store().backup(refresh=True)
            except (sqlite3.Error, OSError):
                STATE["error"] = "Local database could not be updated. Check disk permissions and available space."


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def respond(self, status, value, content_type="application/json"):
        body = (json.dumps(value) if content_type == "application/json" else value).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        return self.local_host() and secrets.compare_digest(self.headers.get("X-App-Token", ""), TOKEN)

    def local_host(self):
        return self.headers.get("Host") in (f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}")

    def do_GET(self):
        if not self.local_host():
            return self.respond(403, {"error": "Local access only"})
        if self.path == "/":
            self.respond(200, Path(__file__).with_name("index.html").read_text(encoding="utf-8").replace("__TOKEN__", TOKEN), "text/html")
        elif self.path == "/app.js":
            self.respond(200, Path(__file__).with_name("app.js").read_text(encoding="utf-8"), "text/javascript")
        elif self.path == "/api/health":
            self.respond(200, {"app": APP_ID})
        elif self.path == "/api/settings" and self.authorized():
            try:
                self.respond(200, store().settings())
            except sqlite3.Error:
                self.respond(500, {"error": "Cannot load saved settings."})
        elif self.path == "/api/status" and self.authorized():
            with LOCK:
                self.respond(200, STATE)
        elif self.path == "/api/history" and self.authorized():
            try:
                self.respond(200, {"items": recent_history()})
            except sqlite3.Error:
                self.respond(500, {"error": "Cannot read the send history database."})
        else:
            self.respond(404, {"error": "Not found"})

    def do_POST(self):
        if not self.authorized():
            return self.respond(403, {"error": "Unauthorized"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                raise ValueError("Request is empty or too large.")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("Expected an object.")
            if self.path == "/api/settings":
                filename, attachment = attachment_from(data) if data.get("attachment") else (None, None)
                store().save_settings(data, attachment, filename)
                return self.respond(200, store().settings())
            if self.path == "/api/stop":
                STOP.set()
                return self.respond(200, {"ok": True})
            if self.path != "/api/send":
                return self.respond(404, {"error": "Not found"})
            config = validate(data)
            with LOCK:
                if STATE["running"]:
                    return self.respond(409, {"error": "A queue is already running."})
                store().save_settings(data, config["attachment"], config["filename"])
                STOP.clear()
                STATE.update(running=True, error="", items=[{"email": address, "status": "Queued", "detail": ""} for address in config["recipients"]])
                persist_state()
                threading.Thread(target=send_queue, args=(config,), daemon=True, name="send-worker").start()
            self.respond(200, {"ok": True})
        except (sqlite3.Error, OSError):
            with LOCK:
                if not any(t.name == "send-worker" and t.is_alive() for t in threading.enumerate()):
                    STATE["running"] = False
            self.respond(500, {"error": "Cannot save local data. Nothing new was started. Check disk space and permissions."})
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            self.respond(400, {"error": str(exc)})


def main():
    url = f"http://127.0.0.1:{PORT}"
    try:
        server = LocalServer(("127.0.0.1", PORT), Handler)
    except OSError:
        from urllib.request import urlopen
        try:
            with urlopen(url + "/api/health", timeout=2) as response:
                if json.load(response).get("app") != APP_ID:
                    raise ValueError("Different app")
        except Exception:
            raise SystemExit(f"Port {PORT} is in use by another app. Close it and try again.")
        webbrowser.open(url)
        print("Opened the already-running Resume Mailer.")
        return
    try:
        initialize_storage()
        with LOCK:
            STATE.update(store().load_queue())
            persist_state()
        print(f"Email automation: {url}\nDatabase: {DB_PATH}\nKeep this window open. Press Ctrl+C to exit.", flush=True)
        webbrowser.open(url)
        server.serve_forever()
    except KeyboardInterrupt:
        STOP.set()
        # Allow the current SMTP operation to finish recording its result.
        for worker in threading.enumerate():
            if worker.name == "send-worker":
                print("Waiting for the current email to finish...", flush=True)
                worker.join()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
