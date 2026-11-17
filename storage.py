"""Local persistence. Passwords use per-user Windows DPAPI, never plaintext."""

import ctypes
import json
import os
import sqlite3
import time
from contextlib import closing, contextmanager
from pathlib import Path

COOLDOWN = 30 * 24 * 60 * 60
FIELDS = ("preset", "sender", "name", "username", "host", "port", "security",
          "delay", "recipients", "subject", "message")


def protect_password(value, decrypt=False):
    if os.name != "nt":
        raise ValueError("Saving passwords requires Windows. You can still enter a password for each send.")
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    raw = value if decrypt else value.encode("utf-8")
    buffer = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise ValueError("Windows could not unlock/save the password. Re-enter it using this Windows account.")
    try:
        output = ctypes.string_at(result.data, result.size)
        return output.decode("utf-8") if decrypt else output
    finally:
        kernel.LocalFree(result.data)


_READY = set()


class Storage:
    def __init__(self, path):
        self.path = Path(path).resolve()

    def migrate_from(self, source_path):
        """Preserve the old project database and merge any recent sends into stable storage."""
        source_path = Path(source_path).resolve()
        if source_path == self.path or not source_path.exists():
            return
        with closing(sqlite3.connect(f"{source_path.as_uri()}?mode=ro", uri=True)) as source:
            if not self.path.exists():
                with closing(sqlite3.connect(self.path)) as destination:
                    source.backup(destination)
            else:
                rows = source.execute("SELECT email, sent_at FROM sent WHERE sent_at > ?", (time.time() - COOLDOWN,)).fetchall()
                with self.connect() as destination:
                    for email, stamp in rows:
                        destination.execute("INSERT INTO sent VALUES (?, ?) ON CONFLICT(email) DO UPDATE SET sent_at=MAX(sent.sent_at, excluded.sent_at)", (email.lower(), stamp))

    @contextmanager
    def connect(self):
        key = str(self.path)
        if not self.path.exists():
            _READY.discard(key)
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            with connection:
                connection.execute("PRAGMA synchronous=FULL")
                if key not in _READY:
                    self.create_schema(connection)
                    _READY.add(key)
                yield connection
        finally:
            connection.close()

    @staticmethod
    def create_schema(connection):
        connection.execute("CREATE TABLE IF NOT EXISTS sent (email TEXT PRIMARY KEY, sent_at REAL NOT NULL)")
        connection.execute("CREATE TABLE IF NOT EXISTS preferences (id INTEGER PRIMARY KEY CHECK (id=1), data TEXT NOT NULL, password BLOB, filename TEXT, attachment BLOB)")
        connection.execute("CREATE TABLE IF NOT EXISTS attempts (email TEXT PRIMARY KEY, updated_at REAL NOT NULL)")
        connection.execute("CREATE TABLE IF NOT EXISTS queue (id INTEGER PRIMARY KEY CHECK (id=1), data TEXT NOT NULL)")

    def backup(self, refresh=False):
        """Keep up to 30 daily snapshots; replace today's copy atomically after a queue."""
        if not self.path.exists():
            return
        target = self.path.with_name(f"history-backup-{time.strftime('%Y-%m-%d')}.sqlite3")
        if refresh or not target.exists():
            temporary = target.with_suffix(".sqlite3.tmp")
            with closing(sqlite3.connect(self.path)) as source, closing(sqlite3.connect(temporary)) as destination:
                source.backup(destination)
            temporary.replace(target)
        # Backups follow the same 30-day retention as live history.
        for item in self.path.parent.glob("history-backup-????-??-??.sqlite3"):
            if item.stat().st_mtime <= time.time() - COOLDOWN:
                item.unlink()

    @staticmethod
    def prune(connection):
        cutoff = time.time() - COOLDOWN
        connection.execute("DELETE FROM sent WHERE sent_at <= ?", (cutoff,))
        connection.execute("DELETE FROM attempts WHERE updated_at <= ?", (cutoff,))

    def history(self):
        with self.connect() as db:
            self.prune(db)
            items = [{"email": email, "sent_at": stamp, "eligible_at": stamp + COOLDOWN, "status": "Sent"}
                     for email, stamp in db.execute("SELECT email, sent_at FROM sent")]
            items += [{"email": email, "sent_at": stamp, "eligible_at": stamp + COOLDOWN, "status": "Uncertain"}
                      for email, stamp in db.execute("SELECT email, updated_at FROM attempts WHERE email NOT IN (SELECT email FROM sent)")]
            return sorted(items, key=lambda row: row["sent_at"], reverse=True)

    def reserve(self, email):
        """Commit an attempt before SMTP, so even an interrupted process leaves a record."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.prune(db)
            if db.execute("SELECT 1 FROM sent WHERE email=? UNION ALL SELECT 1 FROM attempts WHERE email=?", (email.lower(), email.lower())).fetchone():
                return False
            db.execute("INSERT INTO attempts VALUES (?, ?)", (email.lower(), time.time()))
            return True

    def record_sent(self, email):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO sent VALUES (?, ?)", (email.lower(), time.time()))
            db.execute("DELETE FROM attempts WHERE email=?", (email.lower(),))

    def release(self, email):
        with self.connect() as db:
            db.execute("DELETE FROM attempts WHERE email=?", (email.lower(),))

    def save_queue(self, state):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO queue VALUES (1, ?)", (json.dumps(state),))

    def load_queue(self):
        with self.connect() as db:
            row = db.execute("SELECT data FROM queue WHERE id=1").fetchone()
        state = json.loads(row[0]) if row else {"running": False, "items": [], "error": ""}
        was_running = state["running"]
        state["running"] = False
        for item in state["items"]:
            if item["status"] == "Sending":
                item.update(status="Uncertain", detail="App closed during this attempt. Check Gmail Sent before retrying; recipient remains protected.")
            elif item["status"] == "Queued":
                item["status"] = "Stopped"
        if was_running:
            state["error"] = "Previous queue was interrupted. It will not resume automatically."
        return state

    def settings(self):
        with self.connect() as db:
            row = db.execute("SELECT data, password, filename FROM preferences WHERE id=1").fetchone()
        result = json.loads(row[0]) if row else {}
        result.update(has_password=bool(row and row[1]), filename=row[2] if row else "", database=str(self.path))
        return result

    def save_settings(self, data, attachment=None, filename=None):
        clean = {key: str(data[key]) for key in FIELDS if key in data}
        password = data.get("password", "")
        remember = data.get("remember_password", True)
        encrypted = protect_password(password) if password and remember and os.name == "nt" else None
        with self.connect() as db:
            row = db.execute("SELECT data, password, filename, attachment FROM preferences WHERE id=1").fetchone()
            previous = json.loads(row[0]) if row else {}
            old_identity = (previous.get("sender", "").lower(), previous.get("username", "").lower(), previous.get("host", "").lower())
            previous.update(clean)
            new_identity = (previous.get("sender", "").lower(), previous.get("username", "").lower(), previous.get("host", "").lower())
            # Never silently use a saved credential for a different SMTP account/host.
            if not password and remember and row and old_identity == new_identity:
                encrypted = row[1]
            if attachment is None and row:
                filename, attachment = row[2], row[3]
            db.execute("INSERT OR REPLACE INTO preferences VALUES (1, ?, ?, ?, ?)",
                       (json.dumps(previous), encrypted, filename or "", attachment))

    def saved_password(self, data):
        current = self.settings()
        if any(str(data.get(key, "")).lower() != current.get(key, "").lower() for key in ("sender", "username", "host")):
            return ""
        with self.connect() as db:
            row = db.execute("SELECT password FROM preferences WHERE id=1").fetchone()
        return protect_password(row[0], decrypt=True) if row and row[0] else ""

    def saved_resume(self):
        with self.connect() as db:
            row = db.execute("SELECT filename, attachment FROM preferences WHERE id=1").fetchone()
        return (row[0], row[1]) if row and row[1] else ("", b"")
