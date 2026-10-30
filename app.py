import base64
import json
import mimetypes
import re
import secrets
import smtplib
import ssl
import sqlite3
import time
import threading
import webbrowser
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from contextlib import contextmanager

TOKEN = secrets.token_urlsafe(32)
LOCK = threading.Lock()
STOP = threading.Event()
STATE = {"running": False, "items": [], "error": ""}
EMAIL = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}$")
MAX_BODY = 20 * 1024 * 1024
DB_PATH = Path(__file__).with_name("send_history.sqlite3")
COOLDOWN = 30 * 24 * 60 * 60


@contextmanager
def history_connection():
    connection = sqlite3.connect(DB_PATH, timeout=10)
    try:
        with connection:
            connection.execute("CREATE TABLE IF NOT EXISTS sent (email TEXT PRIMARY KEY, sent_at REAL NOT NULL)")
            yield connection
    finally:
        connection.close()


def recent_history():
    with history_connection() as connection:
        connection.execute("DELETE FROM sent WHERE sent_at <= ?", (time.time() - COOLDOWN,))
        return [{"email": email, "sent_at": sent_at, "eligible_at": sent_at + COOLDOWN}
                for email, sent_at in connection.execute("SELECT email, sent_at FROM sent ORDER BY sent_at DESC")]


def record_sent(recipient):
    with history_connection() as connection:
        connection.execute("INSERT OR REPLACE INTO sent VALUES (?, ?)", (recipient.lower(), time.time()))


def validate(data):
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
    attachment = base64.b64decode(data.get("attachment", ""), validate=True)
    if not attachment or len(attachment) > 10 * 1024 * 1024:
        raise ValueError("Attach a resume no larger than 10 MB.")
    filename = Path(data.get("filename", "resume.pdf")).name
    if not filename or any(c in filename for c in "\r\n"):
        raise ValueError("Invalid attachment filename.")
    return {**data, "recipients": recipients, "port": port, "delay": delay, "attachment": attachment, "filename": filename}


def send_queue(config):
    try:
        for index, recipient in enumerate(config["recipients"]):
            if STOP.is_set():
                break
            try:
                previous = next((item for item in recent_history() if item["email"] == recipient.lower()), None)
            except sqlite3.Error:
                with LOCK:
                    STATE["error"] = "Cannot read send history. Queue stopped to prevent duplicate emails."
                break
            if previous:
                with LOCK:
                    STATE["items"][index].update(status="Skipped", detail="Already emailed within the last 30 days.")
                continue
            with LOCK:
                STATE["items"][index]["status"] = "Sending"
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
                connection = (smtplib.SMTP_SSL(config["host"], config["port"], timeout=30, context=context)
                              if config["security"] == "ssl" else smtplib.SMTP(config["host"], config["port"], timeout=30))
                with connection as smtp:
                    if config["security"] == "starttls":
                        smtp.ehlo()
                        smtp.starttls(context=context)
                        smtp.ehlo()
                    smtp.login(config["username"], config["password"])
                    smtp.send_message(message)
                    # Save immediately after acceptance, before SMTP's QUIT can fail.
                    try:
                        record_sent(recipient)
                    except sqlite3.Error:
                        STOP.set()
                        with LOCK:
                            STATE["error"] = "Email accepted, but history could not be saved. Queue stopped; check Sent mail before retrying."
                        raise
                status, detail = "Sent", "Accepted by the SMTP server; delivery is not guaranteed."
            except Exception as exc:
                status, detail = "Failed", str(exc)
                # Authentication errors affect every remaining recipient.
                if isinstance(exc, smtplib.SMTPAuthenticationError):
                    STOP.set()
            with LOCK:
                STATE["items"][index].update(status=status, detail=detail)
            if index < len(config["recipients"]) - 1 and STOP.wait(config["delay"]):
                break
    finally:
        config["password"] = ""
        with LOCK:
            for item in STATE["items"]:
                if item["status"] == "Queued":
                    item["status"] = "Stopped"
            STATE["running"] = False


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
        return secrets.compare_digest(self.headers.get("X-App-Token", ""), TOKEN)

    def do_GET(self):
        if self.path == "/":
            self.respond(200, Path(__file__).with_name("index.html").read_text(encoding="utf-8").replace("__TOKEN__", TOKEN), "text/html")
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
            if self.path == "/api/stop":
                STOP.set()
                return self.respond(200, {"ok": True})
            if self.path != "/api/send":
                return self.respond(404, {"error": "Not found"})
            config = validate(data)
            with LOCK:
                if STATE["running"]:
                    return self.respond(409, {"error": "A queue is already running."})
                STOP.clear()
                STATE.update(running=True, error="", items=[{"email": address, "status": "Queued", "detail": ""} for address in config["recipients"]])
                threading.Thread(target=send_queue, args=(config,), daemon=True).start()
            self.respond(200, {"ok": True})
        except (ValueError, TypeError, KeyError) as exc:
            self.respond(400, {"error": str(exc)})


if __name__ == "__main__":
    recent_history()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"Email automation: {url}\nKeep this window open. Press Ctrl+C to exit.")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        STOP.set()
        server.server_close()
