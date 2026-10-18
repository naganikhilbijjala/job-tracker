"""Where the data lives.

* Locally: a SQLite file at data/tracker.db, resume files in data/resumes/.
* Hosted: Turso (hosted SQLite) when TURSO_DATABASE_URL is set. Serverless hosts
  have no lasting disk, so resume files are stored in the database as well.
"""
import base64
import http.client
import json
import os
import sqlite3
import threading
import urllib.parse
from pathlib import Path

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS resumes (
         id          INTEGER PRIMARY KEY,
         label       TEXT NOT NULL,
         filename    TEXT NOT NULL UNIQUE,
         size        INTEGER,
         uploaded_at TEXT NOT NULL,
         drive_id    TEXT,
         drive_link  TEXT)""",
    """CREATE TABLE IF NOT EXISTS jobs (
         id           INTEGER PRIMARY KEY,
         company      TEXT NOT NULL DEFAULT '',
         title        TEXT NOT NULL DEFAULT '',
         location     TEXT NOT NULL DEFAULT '',
         url          TEXT NOT NULL DEFAULT '',
         source       TEXT NOT NULL DEFAULT '',
         salary       TEXT NOT NULL DEFAULT '',
         status       TEXT NOT NULL DEFAULT 'saved',
         description  TEXT NOT NULL DEFAULT '',
         notes        TEXT NOT NULL DEFAULT '',
         resume_id    INTEGER,
         date_applied TEXT,
         follow_up    TEXT,
         created_at   TEXT NOT NULL,
         updated_at   TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS jobs_url ON jobs(url)",
    """CREATE TABLE IF NOT EXISTS events (
         id     INTEGER PRIMARY KEY,
         job_id INTEGER NOT NULL,
         at     TEXT NOT NULL,
         kind   TEXT NOT NULL,
         detail TEXT NOT NULL DEFAULT '')""",
    "CREATE INDEX IF NOT EXISTS events_job ON events(job_id)",
    "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS resume_files (resume_id INTEGER PRIMARY KEY, data BLOB NOT NULL)",
    """CREATE TABLE IF NOT EXISTS chat_messages (
         id         INTEGER PRIMARY KEY,
         job_id     INTEGER NOT NULL,
         role       TEXT NOT NULL,
         content    TEXT NOT NULL,
         created_at TEXT NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS chat_job ON chat_messages(job_id)",
    """CREATE TABLE IF NOT EXISTS generated_resumes (
         id         INTEGER PRIMARY KEY,
         job_id     INTEGER,
         title      TEXT NOT NULL,
         content    TEXT NOT NULL,
         source     TEXT NOT NULL DEFAULT 'app',
         created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS resume_reviews (
         generated_id INTEGER PRIMARY KEY,
         content_hash TEXT NOT NULL,
         result       TEXT NOT NULL,
         created_at   TEXT NOT NULL)""",
    # Multi-user mode: people sign in with Google. Status is pending (asked for access),
    # active, or blocked. User 1 is always the admin, who owns the data from before.
    """CREATE TABLE IF NOT EXISTS users (
         id         INTEGER PRIMARY KEY,
         email      TEXT NOT NULL UNIQUE,
         name       TEXT NOT NULL DEFAULT '',
         status     TEXT NOT NULL DEFAULT 'pending',
         note       TEXT NOT NULL DEFAULT '',
         created_at TEXT NOT NULL,
         last_login TEXT)""",
    """CREATE TABLE IF NOT EXISTS user_settings (
         user_id INTEGER NOT NULL,
         key     TEXT NOT NULL,
         value   TEXT NOT NULL,
         PRIMARY KEY (user_id, key))""",
]

# Settings that belong to one person (the rest of `settings` is app-wide).
USER_SETTING_KEYS = ("profile", "ai_instructions", "ai_config", "mcp_token", "google_token", "google_folder")
OWNED_TABLES = ("jobs", "resumes", "generated_resumes")


def _migrate(db):
    """Bring a database from before multi-user up to date. Safe to run every start."""
    for table in OWNED_TABLES:
        try:
            db.execute(f"ALTER TABLE {table} ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1")
        except Exception as e:  # sqlite3 and Turso both say "duplicate column name"
            if "duplicate column" not in str(e).lower():
                raise
    marks = ", ".join("?" * len(USER_SETTING_KEYS))
    db.run_many([
        *[(f"CREATE INDEX IF NOT EXISTS {t}_user ON {t}(user_id)", ()) for t in OWNED_TABLES],
        (f"INSERT OR IGNORE INTO user_settings (user_id, key, value) SELECT 1, key, value FROM settings "
         f"WHERE key IN ({marks})", USER_SETTING_KEYS),
        (f"DELETE FROM settings WHERE key IN ({marks})", USER_SETTING_KEYS),
    ])


class DatabaseError(Exception):
    pass


class _Database:
    def run_many(self, statements):
        """Run (sql, params) pairs in order; returns [(rows, lastrowid)] per statement."""
        raise NotImplementedError

    def query(self, sql, params=()):
        return self.run_many([(sql, params)])[0][0]

    def execute(self, sql, params=()):
        return self.run_many([(sql, params)])[0][1]

    def create_schema(self):
        self.run_many([(sql, ()) for sql in SCHEMA])
        _migrate(self)

    def get_setting(self, key):
        rows = self.query("SELECT value FROM settings WHERE key = ?", (key,))
        return rows[0]["value"] if rows else None

    def set_setting(self, key, value):
        if value is None:
            self.execute("DELETE FROM settings WHERE key = ?", (key,))
        else:
            self.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


class UserDB:
    """One person's view of the database: settings are theirs, and `uid` scopes queries.

    Every query on jobs, resumes or generated_resumes must still filter on user_id = db.uid
    (or go through a find_* helper that does)."""

    def __init__(self, db, uid):
        self.db, self.uid = db, uid

    def run_many(self, statements):
        return self.db.run_many(statements)

    def query(self, sql, params=()):
        return self.db.query(sql, params)

    def execute(self, sql, params=()):
        return self.db.execute(sql, params)

    def get_setting(self, key):
        rows = self.db.query("SELECT value FROM user_settings WHERE user_id = ? AND key = ?", (self.uid, key))
        return rows[0]["value"] if rows else None

    def set_setting(self, key, value):
        if value is None:
            self.db.execute("DELETE FROM user_settings WHERE user_id = ? AND key = ?", (self.uid, key))
        else:
            self.db.execute("INSERT INTO user_settings (user_id, key, value) VALUES (?, ?, ?) "
                            "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value", (self.uid, key, value))


class LocalDB(_Database):
    def __init__(self, path):
        self.path = path

    def run_many(self, statements):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                out = []
                for sql, params in statements:
                    cur = conn.execute(sql, params)
                    out.append(([dict(r) for r in cur.fetchall()], cur.lastrowid))
                return out
        finally:
            conn.close()


def _encode(value):
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    if isinstance(value, (bytes, bytearray)):
        return {"type": "blob", "base64": base64.b64encode(value).decode()}
    return {"type": "text", "value": str(value)}


def _decode(cell):
    kind = cell.get("type")
    if kind == "integer":
        return int(cell["value"])
    if kind == "float":
        return float(cell["value"])
    if kind == "blob":
        return base64.b64decode(cell["base64"] + "=" * (-len(cell["base64"]) % 4))
    if kind == "null":
        return None
    return cell.get("value")


class TursoDB(_Database):
    """Talks to Turso over its HTTP pipeline API, reusing one HTTPS connection per thread."""

    def __init__(self, url, token):
        parts = urllib.parse.urlsplit(url.replace("libsql://", "https://", 1))
        self.host = parts.netloc
        # http:// is for a local libSQL dev server; Turso itself is always https.
        self.connection_class = http.client.HTTPConnection if parts.scheme == "http" else http.client.HTTPSConnection
        self.token = token
        self._local = threading.local()

    def _post(self, payload):
        body = json.dumps(payload).encode()
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
        for attempt in range(2):
            conn = getattr(self._local, "conn", None) or self.connection_class(self.host, timeout=20)
            self._local.conn = conn
            try:
                conn.request("POST", "/v2/pipeline", body, headers)
                res = conn.getresponse()
                data = res.read()
            except (http.client.HTTPException, OSError) as e:
                conn.close()
                self._local.conn = None
                if attempt:  # a kept-alive connection may have gone stale; retry once on a fresh one
                    raise DatabaseError(f"Couldn't reach the database: {e}") from None
                continue
            if res.status == 401:
                raise DatabaseError("The database rejected the token — check TURSO_AUTH_TOKEN")
            if res.status != 200:
                raise DatabaseError(f"Database error {res.status}: {data[:300].decode('utf-8', 'replace')}")
            return json.loads(data)

    def run_many(self, statements):
        requests = [{"type": "execute", "stmt": {"sql": sql, "args": [_encode(p) for p in params]}}
                    for sql, params in statements]
        results = self._post({"requests": requests + [{"type": "close"}]})["results"]
        out = []
        for r in results[:len(statements)]:
            if r.get("type") != "ok":
                raise DatabaseError((r.get("error") or {}).get("message", "Query failed"))
            res = r["response"]["result"]
            cols = [c["name"] for c in res["cols"]]
            rows = [dict(zip(cols, map(_decode, row))) for row in res["rows"]]
            rowid = res.get("last_insert_rowid")
            out.append((rows, int(rowid) if rowid is not None else None))
        return out


class FolderFiles:
    """Resume files as plain files in a folder (local mode)."""
    kind = "folder"

    def __init__(self, folder):
        self.folder = folder

    def exists(self, filename):
        return (self.folder / filename).exists()

    def missing(self, resume):
        return not (self.folder / resume["filename"]).exists()

    def save(self, resume_id, filename, data):
        (self.folder / filename).write_bytes(data)

    def read(self, resume_id, filename):
        path = self.folder / filename
        return path.read_bytes() if path.is_file() else None

    def delete(self, resume_id, filename):
        (self.folder / filename).unlink(missing_ok=True)


class DatabaseFiles:
    """Resume files stored as blobs in the database (hosted mode)."""
    kind = "database"

    def __init__(self, db):
        self.db = db

    def exists(self, filename):
        return bool(self.db.query("SELECT 1 FROM resumes WHERE filename = ?", (filename,)))

    def missing(self, resume):
        return False

    def save(self, resume_id, filename, data):
        self.db.execute("INSERT OR REPLACE INTO resume_files (resume_id, data) VALUES (?, ?)", (resume_id, data))

    def read(self, resume_id, filename):
        rows = self.db.query("SELECT data FROM resume_files WHERE resume_id = ?", (resume_id,))
        return rows[0]["data"] if rows else None

    def delete(self, resume_id, filename):
        self.db.execute("DELETE FROM resume_files WHERE resume_id = ?", (resume_id,))


def open_backend(data_dir: Path):
    """Return (db, files) for this environment."""
    url = os.environ.get("TURSO_DATABASE_URL")
    if url:
        db = TursoDB(url, os.environ.get("TURSO_AUTH_TOKEN", ""))
        files = DatabaseFiles(db)
    elif os.environ.get("VERCEL"):
        raise DatabaseError("TURSO_DATABASE_URL isn't set. Add it (and TURSO_AUTH_TOKEN) in the "
                            "Vercel project's Environment Variables, then redeploy.")
    else:
        (data_dir / "resumes").mkdir(parents=True, exist_ok=True)
        db = LocalDB(data_dir / "tracker.db")
        files = FolderFiles(data_dir / "resumes")
    db.create_schema()
    return db, files
