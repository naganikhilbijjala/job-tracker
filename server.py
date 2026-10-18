#!/usr/bin/env python3
"""Job Tracker — a small, dependency-free job application tracker.

Locally:   python3 server.py        (then open http://127.0.0.1:8765)
Vercel:    loads the WSGI `app` defined below.
"""
import csv
import hashlib
import hmac
import io
import json
import mimetypes
import os
import re
import secrets
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import date, datetime, timedelta
from html import unescape
from html.parser import HTMLParser
from http import HTTPStatus
from http.cookies import SimpleCookie
from pathlib import Path
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

import ai
import database
import gdrive
import mcp
import scoring

ROOT = Path(__file__).resolve().parent
PUBLIC = ROOT / "public"
DATA = ROOT / "data"
HOST = "127.0.0.1"
PORT = int(os.environ.get("JOB_TRACKER_PORT", 8765))
LOCAL_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
ON_VERCEL = bool(os.environ.get("VERCEL"))
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
# Multi-user mode: people sign in with Google and the admin decides who gets in.
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "").strip().lower()
MULTI_USER = bool(ADMIN_EMAIL) and gdrive.is_configured()
ADMIN_ID = 1  # the admin is always user 1, who also owns everything from before multi-user
# Sessions are signed with a key derived from this, so changing it logs everyone out.
_SECRET_SOURCE = (os.environ.get("SESSION_SECRET") or APP_PASSWORD
                  or (gdrive.client_secret() if MULTI_USER else ""))
SECRET = (hashlib.sha256(b"job-tracker:" + _SECRET_SOURCE.encode()).digest()
          if _SECRET_SOURCE else secrets.token_bytes(32))
SESSION_COOKIE = "jt_session"
REQUEST_COOKIE = "jt_request"  # lets someone waiting for approval see and add to their request
LOGIN_COOKIE = "jt_login"  # set while a Google sign-in is in progress
SESSION_DAYS = 180

STATUSES = ["saved", "applied", "interviewing", "offer", "rejected", "withdrawn"]
JOB_FIELDS = ["company", "title", "location", "url", "source", "salary", "status",
              "description", "notes", "resume_id", "date_applied", "follow_up"]
RESUME_EXTS = {".pdf", ".doc", ".docx", ".odt", ".rtf", ".txt", ".md"}
MAX_RESUME_BYTES = 4 * 1024 * 1024  # Vercel caps request bodies at 4.5 MB
FOLLOW_UP_DAYS = 7
# Query params that only track clicks; stripped so the same posting dedupes.
TRACKING_PARAMS = re.compile(r"^(utm_.*|trk.*|ref|refid|src|source|gh_src|lever-source.*|"
                             r"trackingid|eBP|from|fbclid|gclid|mc_.*|_hs.*)$", re.I)


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


_request = threading.local()


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def today():
    """The user's local date (sent by the browser), since the host may run on UTC."""
    return getattr(_request, "today", None) or date.today().isoformat()


# ---------------------------------------------------------------- storage

_backend = None
_backend_lock = threading.Lock()


def store():
    """(db, files) for the whole app, opened on first use so a cold start only pays for it when needed."""
    global _backend
    with _backend_lock:
        if _backend is None:
            _backend = database.open_backend(DATA)
        return _backend


def backend():
    """(db, files) for the signed-in user. db.uid scopes every query on owned tables."""
    uid = getattr(_request, "uid", None)
    if not uid:
        raise ApiError(401, "Please log in")
    db, files = store()
    return database.UserDB(db, uid), files


def log_event(job_id, kind, detail=""):
    return ("INSERT INTO events (job_id, at, kind, detail) VALUES (?, ?, ?, ?)", (job_id, now(), kind, detail))


# Callers append " AND …" conditions; the first parameter is always db.uid.
JOB_LIST_SQL = """
SELECT j.id, j.company, j.title, j.location, j.url, j.source, j.salary, j.status,
       j.notes, j.resume_id, r.label AS resume_label, j.date_applied, j.follow_up,
       j.created_at, j.updated_at, length(j.description) AS desc_len
FROM jobs j LEFT JOIN resumes r ON r.id = j.resume_id AND r.user_id = j.user_id
WHERE j.user_id = ?
"""


def job_summary(db, job_id):
    rows = db.query(JOB_LIST_SQL + " AND j.id = ?", (db.uid, job_id))
    return rows[0] if rows else None


# The find_* helpers are the ownership check: anything fetched by id goes through one first.
def find_job(db, job_id):
    rows = db.query("SELECT * FROM jobs WHERE id = ? AND user_id = ?", (job_id, db.uid))
    if not rows:
        raise ApiError(404, "Job not found")
    return rows[0]


def check_resume_owner(db, fields):
    if fields.get("resume_id"):
        find_resume(db, fields["resume_id"])


def normalize_url(url):
    url = (url or "").strip()
    if not url:
        return ""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return url
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    # LinkedIn search pages carry the posting in ?currentJobId=… — canonicalize.
    if "linkedin.com" in parts.netloc:
        job_id = dict(query).get("currentJobId")
        m = re.search(r"/jobs/view/(?:[^/]*-)?(\d+)", parts.path)
        job_id = job_id or (m and m.group(1))
        if job_id:
            return f"https://www.linkedin.com/jobs/view/{job_id}/"
    query = [(k, v) for k, v in query if not TRACKING_PARAMS.match(k)]
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc.lower(), parts.path,
                                    urllib.parse.urlencode(query), ""))


def clean_job(data):
    out = {}
    for key in JOB_FIELDS:
        if key not in data:
            continue
        value = data[key]
        if key == "resume_id":
            value = int(value) if value not in (None, "") else None
        elif key in ("date_applied", "follow_up"):
            value = str(value).strip() if value else None
            if value and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ApiError(400, f"{key} must be YYYY-MM-DD")
        else:
            value = "" if value is None else str(value).strip()
        out[key] = value
    if "status" in out and out["status"] not in STATUSES:
        raise ApiError(400, f"Unknown status '{out['status']}'")
    if "url" in out:
        out["url"] = normalize_url(out["url"])
    return out


def status_side_effects(fields, old):
    """Moving a job into an active stage stamps the applied date and a follow-up reminder."""
    new = fields.get("status")
    if not new or (old and old["status"] == new) or new not in ("applied", "interviewing", "offer"):
        return
    applied = fields.get("date_applied") or (old["date_applied"] if old else None)
    if not applied:
        fields["date_applied"] = applied = today()
    if new == "applied" and not (fields.get("follow_up") or (old and old["follow_up"])):
        fields["follow_up"] = (date.fromisoformat(applied) + timedelta(days=FOLLOW_UP_DAYS)).isoformat()


def insert_job(db, fields):
    fields.setdefault("status", "saved")
    if fields.get("url") and not fields.get("source"):
        fields["source"] = urllib.parse.urlsplit(fields["url"]).netloc.removeprefix("www.")
    check_resume_owner(db, fields)
    status_side_effects(fields, None)
    fields["user_id"] = db.uid
    fields["created_at"] = fields["updated_at"] = now()
    cols = ", ".join(fields)
    marks = ", ".join("?" * len(fields))
    job_id = db.execute(f"INSERT INTO jobs ({cols}) VALUES ({marks})", list(fields.values()))
    db.execute(*log_event(job_id, "created", fields["status"]))
    return job_id


def update_job(db, job_id, fields):
    old = find_job(db, job_id)
    check_resume_owner(db, fields)
    status_side_effects(fields, old)
    if not fields:
        return
    fields["updated_at"] = now()
    sets = ", ".join(f"{k} = ?" for k in fields)
    statements = [(f"UPDATE jobs SET {sets} WHERE id = ? AND user_id = ?", [*fields.values(), job_id, db.uid])]
    if "status" in fields and fields["status"] != old["status"]:
        statements.append(log_event(job_id, "status", f"{old['status']} → {fields['status']}"))
    if fields.get("resume_id") and fields["resume_id"] != old["resume_id"]:
        statements.append(("INSERT INTO events (job_id, at, kind, detail) "
                           "SELECT ?, ?, 'resume', label FROM resumes WHERE id = ? AND user_id = ?",
                           (job_id, now(), fields["resume_id"], db.uid)))
    db.run_many(statements)


def sync_resume_folder(db, files):
    """Pick up files dropped straight into data/resumes/ (local, single-user mode only)."""
    if files.kind != "folder" or db.uid != ADMIN_ID:
        return
    known = {r["filename"] for r in db.query("SELECT filename FROM resumes")}
    for p in sorted(files.folder.iterdir()):
        if p.is_file() and p.suffix.lower() in RESUME_EXTS and p.name not in known:
            st = p.stat()
            db.execute("INSERT INTO resumes (user_id, label, filename, size, uploaded_at) VALUES (?, ?, ?, ?, ?)",
                       (db.uid, p.stem, p.name, st.st_size,
                        datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")))


def list_resumes(db, files, only_id=None):
    sync_resume_folder(db, files)
    rows = db.query("""
        SELECT r.*, (SELECT count(*) FROM jobs j WHERE j.resume_id = r.id AND j.user_id = r.user_id) AS job_count
        FROM resumes r WHERE r.user_id = ? ORDER BY r.uploaded_at DESC""", (db.uid,))
    out = [{**r, "missing": files.missing(r)} for r in rows]
    if only_id is not None:
        return next((r for r in out if r["id"] == int(only_id)), None)
    return out


def find_resume(db, rid):
    rows = db.query("SELECT * FROM resumes WHERE id = ? AND user_id = ?", (rid, db.uid))
    if not rows:
        raise ApiError(404, "Resume not found")
    return rows[0]


def safe_filename(name, files):
    name = Path(name.replace("\\", "/")).name
    stem, ext = os.path.splitext(name)
    ext = ext.lower()
    if ext not in RESUME_EXTS:
        raise ApiError(400, f"Unsupported file type '{ext}'. Use one of: {', '.join(sorted(RESUME_EXTS))}")
    stem = re.sub(r"[^\w.\- ()]+", "_", stem).strip(" .") or "resume"
    candidate, n = stem + ext, 1
    while files.exists(candidate):
        n += 1
        candidate = f"{stem} ({n}){ext}"
    return candidate


def push_to_drive(db, files, resume):
    content = files.read(resume["id"], resume["filename"])
    if content is None:
        raise ApiError(404, f"{resume['filename']} is missing")
    mime = mimetypes.guess_type(resume["filename"])[0] or "application/octet-stream"
    file_id, link = gdrive.upload(db, content, resume["filename"], mime)
    db.execute("UPDATE resumes SET drive_id = ?, drive_link = ? WHERE id = ? AND user_id = ?",
               (file_id, link, resume["id"], db.uid))


# ---------------------------------------------------------------- job page parsing

class PageParser(HTMLParser):
    """Collects JSON-LD blocks, <meta> tags, <title> and the first <h1>."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ld, self.meta, self.title, self.h1 = [], {}, "", ""
        self._tag, self._buf = None, []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "script" and (a.get("type") or "").lower() == "application/ld+json":
            self._tag, self._buf = tag, []
        elif (tag == "title" and not self.title) or (tag == "h1" and not self.h1):
            self._tag, self._buf = tag, []
        elif tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key and a.get("content") and key not in self.meta:
                self.meta[key] = a["content"].strip()

    def handle_data(self, data):
        if self._tag:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag != self._tag:
            return
        text = "".join(self._buf).strip()
        if tag == "script":
            self.ld.append(text)
        elif tag == "title":
            self.title = " ".join(text.split())
        else:
            self.h1 = " ".join(text.split())
        self._tag = None


class TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "br", "ul", "ol", "section", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []

    def handle_starttag(self, tag, attrs):
        if tag == "li":
            self.out.append("\n• ")
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self.BLOCK or tag == "li":
            self.out.append("\n")

    def handle_data(self, data):
        self.out.append(data)


def tidy_text(raw):
    lines = [" ".join(line.split()) for line in raw.splitlines()]
    text = re.sub(r"•\n+", "• ", "\n".join(lines))  # <li><p>…</p></li> leaves the bullet on its own line
    text = re.sub(r"^• *$", "", text, flags=re.M)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def html_to_text(s):
    if not s:
        return ""
    if "<" not in s and "&lt;" in s:  # some sites double-escape the HTML
        s = unescape(s)
    p = TextExtractor()
    p.feed(s)
    return tidy_text("".join(p.out))


def find_posting(node):
    if isinstance(node, list):
        for n in node:
            if found := find_posting(n):
                return found
    elif isinstance(node, dict):
        t = node.get("@type")
        if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
            return node
        if "@graph" in node:
            return find_posting(node["@graph"])
    return None


def format_salary(base):
    if not isinstance(base, dict):
        return ""
    value = base.get("value")
    unit, lo, hi, single = "", None, None, value
    if isinstance(value, dict):
        unit = value.get("unitText") or ""
        lo, hi, single = value.get("minValue"), value.get("maxValue"), value.get("value")

    def fmt(x):
        try:
            return f"{float(x):,.0f}"
        except (TypeError, ValueError):
            return str(x)

    if lo and hi and lo != hi:
        amount = f"{fmt(lo)}–{fmt(hi)}"
    elif lo or hi or single:
        amount = fmt(lo or hi or single)
    else:
        return ""
    return " ".join(p for p in (base.get("currency", ""), amount, f"/ {unit.lower()}" if unit else "") if p)


def posting_fields(jp):
    org = jp.get("hiringOrganization")
    company = org.get("name", "") if isinstance(org, dict) else (org if isinstance(org, str) else "")
    locations = jp.get("jobLocation") or []
    if isinstance(locations, dict):
        locations = [locations]
    places = []
    for loc in locations:
        addr = loc.get("address", {}) if isinstance(loc, dict) else loc
        if isinstance(addr, str):
            places.append(addr)
            continue
        country = addr.get("addressCountry")
        if isinstance(country, dict):
            country = country.get("name")
        places.append(", ".join(p for p in (addr.get("addressLocality"), addr.get("addressRegion"), country) if p))
    if "TELECOMMUTE" in str(jp.get("jobLocationType", "")):
        places.insert(0, "Remote")
    return {
        "title": unescape(str(jp.get("title") or "")).strip(),
        "company": unescape(company).strip(),
        "location": "; ".join(dict.fromkeys(p for p in places if p)),
        "salary": format_salary(jp.get("baseSalary")),
        "description": html_to_text(jp.get("description", "")),
    }


def parse_job_page(page, url):
    parser = PageParser()
    parser.feed(page)
    fields = {"url": normalize_url(url),
              "source": urllib.parse.urlsplit(url).netloc.removeprefix("www.")}
    for block in parser.ld:
        try:
            data = json.loads(block)
        except ValueError:
            continue
        if posting := find_posting(data):
            fields.update({k: v for k, v in posting_fields(posting).items() if v})
            break
    meta = parser.meta
    fields.setdefault("title", meta.get("og:title") or parser.h1 or parser.title)
    fields.setdefault("company", meta.get("og:site_name", ""))
    fields.setdefault("description", meta.get("og:description") or meta.get("description", ""))
    return fields


def fetch_greenhouse(url, board, job_id):
    """Greenhouse's newer job boards render client-side, so read their public jobs API instead."""
    api = (f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}"
           "?content=true&pay_transparency=true")
    try:
        with urllib.request.urlopen(api, timeout=15) as res:
            d = json.load(res)
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None
    salary = ""
    for pay in d.get("pay_input_ranges") or []:
        lo, hi = pay.get("min_cents", 0) / 100, pay.get("max_cents", 0) / 100
        salary = f"{pay.get('currency_type', '')} {lo:,.0f}–{hi:,.0f}".strip()
        break
    return {
        "url": normalize_url(url), "source": urllib.parse.urlsplit(url).netloc,
        "title": d.get("title", ""), "company": d.get("company_name", ""),
        "location": (d.get("location") or {}).get("name", ""), "salary": salary,
        "description": html_to_text(d.get("content", "")),
    }


VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}


class ClassTextParser(HTMLParser):
    """Collects the text inside the first element carrying each of the given CSS classes."""

    def __init__(self, classes):
        super().__init__(convert_charrefs=True)
        self.found = {}
        self._wanted = set(classes)
        self._open = []  # [class, depth, chunks] for elements being captured

    def handle_starttag(self, tag, attrs):
        if tag in VOID_TAGS:  # <br>, <img>… never get an end tag, so they don't nest
            for cap in self._open:
                cap[2].append("\n" if tag == "br" else "")
            return
        for cap in self._open:
            cap[1] += 1
            if tag == "li":
                cap[2].append("\n• ")
            elif tag in TextExtractor.BLOCK:
                cap[2].append("\n")
        classes = set((dict(attrs).get("class") or "").split())
        for cls in (self._wanted & classes) - self.found.keys() - {c[0] for c in self._open}:
            self._open.append([cls, 1, []])

    def handle_endtag(self, tag):
        for cap in list(self._open):
            if tag in TextExtractor.BLOCK or tag == "li":
                cap[2].append("\n")
            cap[1] -= 1
            if cap[1] == 0:
                self._open.remove(cap)
                self.found[cap[0]] = tidy_text("".join(cap[2]))

    def handle_data(self, data):
        for cap in self._open:
            cap[2].append(data)


def fetch_linkedin(url, job_id):
    """LinkedIn often answers anonymous visitors with a login wall, but its guest job API
    (what the public job pages are built from) returns the posting without logging in.
    The guest API is rate limited, so fall back to the public job page, which shares its markup."""
    for page in (f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}",
                 f"https://www.linkedin.com/jobs/view/{job_id}/"):
        try:
            raw = fetch_page(page)
        except ApiError:
            continue
        p = ClassTextParser(["top-card-layout__title", "topcard__org-name-link", "topcard__flavor--bullet",
                             "compensation__salary", "show-more-less-html__markup"])
        p.feed(raw)
        if p.found.get("top-card-layout__title"):
            return {
                "url": normalize_url(url), "source": "linkedin.com",
                "title": p.found["top-card-layout__title"],
                "company": p.found.get("topcard__org-name-link", ""),
                "location": p.found.get("topcard__flavor--bullet", ""),
                "salary": p.found.get("compensation__salary", ""),
                "description": p.found.get("show-more-less-html__markup", ""),
            }
    return None


LOGIN_PAGE = re.compile(r"\b(sign ?in|log ?in|sign ?up|join now|authwall)\b", re.I)


def fetch_page(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/128.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            raw = res.read(5_000_000)
            charset = res.headers.get_content_charset() or "utf-8"
            final_url = res.geturl()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 429, 999):  # 999 is LinkedIn's "go away"
            raise ApiError(502, "That site blocked the request. Use the browser extension on the "
                                "posting instead — it reads the page you're already logged into.") from None
        raise ApiError(502, f"The site answered {e.code}.") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise ApiError(502, f"Couldn't load that page: {getattr(e, 'reason', e)}") from None
    if "authwall" in final_url or re.search(r"/(login|signin|uas/login)\b", urllib.parse.urlsplit(final_url).path):
        raise ApiError(502, "That site showed a login page instead of the job. Use the browser "
                            "extension on the posting instead.")
    return raw.decode(charset, "replace")


def fetch_job(url):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ApiError(400, "Enter a full http(s) link to the job posting")
    if parts.netloc.endswith("greenhouse.io"):
        m = re.match(r"/([^/]+)/jobs/(\d+)", parts.path)
        if m and (found := fetch_greenhouse(url, *m.groups())):
            return found
    if parts.netloc.endswith("linkedin.com"):
        m = re.search(r"/jobs/view/(?:[^/]*-)?(\d+)", urllib.parse.urlsplit(normalize_url(url)).path)
        if not m:
            raise ApiError(400, "Use the link to a single LinkedIn job (linkedin.com/jobs/view/…).")
        if found := fetch_linkedin(url, m.group(1)):
            return found
        raise ApiError(502, "LinkedIn didn't share this posting (it may be closed, or LinkedIn is limiting "
                            "requests). Use the browser extension on the posting instead.")
    fields = parse_job_page(fetch_page(url), url)
    if not fields.get("location") and LOGIN_PAGE.search(fields.get("title", "")):
        raise ApiError(502, "That site showed a login page instead of the job. Use the browser "
                            "extension on the posting instead.")
    return fields


# ---------------------------------------------------------------- auth

def sign(payload):
    mac = hmac.new(SECRET, payload.encode(), hashlib.sha256).hexdigest()[:40]
    return f"{payload}.{mac}"


def verify(token, prefix):
    """Check a token made by sign(f"{prefix}:{expiry}:...")."""
    payload, _, mac = (token or "").rpartition(".")
    expected = hmac.new(SECRET, payload.encode(), hashlib.sha256).hexdigest()[:40]
    parts = payload.split(":")
    return (hmac.compare_digest(mac, expected) and parts[0] == prefix
            and len(parts) > 1 and parts[1].isdigit() and int(parts[1]) > time.time())


def session_token(uid, days=SESSION_DAYS):
    return sign(f"session:{int(time.time()) + days * 86400}:{uid}")


def token_user(token):
    """The user id a session token was issued to, or None if it's invalid or expired."""
    if not verify(token, "session"):
        return None
    parts = token.rpartition(".")[0].split(":")
    return int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else ADMIN_ID  # pre-multi-user tokens


def is_active(uid):
    if uid == ADMIN_ID:
        return True
    return bool(store()[0].query("SELECT 1 FROM users WHERE id = ? AND status = 'active'", (uid,)))


def request_user(req):
    """Who is making this request: a user id, or None if nobody is logged in."""
    if not APP_PASSWORD and not MULTI_USER:
        return None if ON_VERCEL else ADMIN_ID  # a public deployment must never run without a login
    auth = req.header("Authorization")
    uid = token_user(auth[7:] if auth.startswith("Bearer ") else req.cookie(SESSION_COOKIE))
    if uid is None or (MULTI_USER and not is_active(uid)):  # removed users are logged out at once
        return None
    return uid


def is_admin():
    return getattr(_request, "uid", None) == ADMIN_ID


def require_admin():
    if not MULTI_USER or not is_admin():
        raise ApiError(403, "Only the admin can do that")


# ---------------------------------------------------------------- HTTP plumbing

class Request:
    def __init__(self, environ):
        self.environ = environ
        self.method = environ["REQUEST_METHOD"].upper()
        # WSGI hands over the path as latin-1-decoded bytes; recover the UTF-8.
        self.path = (environ.get("PATH_INFO") or "/").encode("latin-1").decode("utf-8", "replace")
        self.query = dict(urllib.parse.parse_qsl(environ.get("QUERY_STRING", "")))
        self.host = environ.get("HTTP_HOST", "")
        self.scheme = (environ.get("HTTP_X_FORWARDED_PROTO") or environ.get("wsgi.url_scheme", "http")).split(",")[0]

    @property
    def origin(self):
        return f"{self.scheme}://{self.host}"

    def header(self, name):
        key = name.upper().replace("-", "_")
        return self.environ.get(key if key in ("CONTENT_TYPE", "CONTENT_LENGTH") else "HTTP_" + key, "")

    def cookie(self, name):
        jar = SimpleCookie()
        try:
            jar.load(self.environ.get("HTTP_COOKIE", ""))
        except Exception:
            return ""
        return jar[name].value if name in jar else ""

    def body(self, limit=2_000_000):
        length = int(self.environ.get("CONTENT_LENGTH") or 0)
        if length > limit:
            raise ApiError(413, f"Too large (max {limit // 1_000_000} MB)")
        return self.environ["wsgi.input"].read(length) if length else b""

    def json(self):
        try:
            data = json.loads(self.body() or b"{}")
        except ValueError:
            raise ApiError(400, "Invalid JSON") from None
        if not isinstance(data, dict):
            raise ApiError(400, "Expected a JSON object")
        return data


def response(body, content_type, status=200, headers=()):
    return status, [("Content-Type", content_type), ("Content-Length", str(len(body))),
                    ("Cache-Control", "no-store"), *headers], body


def json_response(data, status=200, headers=()):
    return response(json.dumps(data).encode(), "application/json", status, headers)


def redirect(location):
    return response(b"", "text/plain", 302, [("Location", location)])


def file_response(content, filename, disposition):
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return response(content, ctype, headers=[
        ("Content-Disposition", f"{disposition}; filename*=UTF-8''{urllib.parse.quote(filename)}")])


ROUTES = []


def route(method, pattern, public=False):
    def register(fn):
        ROUTES.append((method, re.compile(pattern), fn, public))
        return fn
    return register


def check_request(req):
    # Locally: only answer to our own hostname (blocks DNS rebinding).
    if not ON_VERCEL and req.host not in LOCAL_HOSTS:
        raise ApiError(403, "Unexpected Host header")
    # Writes must come from our own page or a browser extension, never another website.
    origin = req.header("Origin")
    if req.method not in ("GET", "HEAD") and origin and origin != req.origin \
            and not origin.startswith(("chrome-extension://", "moz-extension://")):
        raise ApiError(403, "Cross-origin request blocked")


def serve_public(path):
    rel = "index.html" if path in ("", "/") else path.lstrip("/")
    target = (PUBLIC / rel).resolve()
    if not target.is_relative_to(PUBLIC.resolve()) or not target.is_file():
        raise ApiError(404, "Not found")
    ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    if target.suffix == ".webmanifest":
        ctype = "application/manifest+json"
    return response(target.read_bytes(), ctype)


def dispatch(req):
    check_request(req)
    method = "GET" if req.method == "HEAD" else req.method
    for m, pattern, fn, public in ROUTES:
        if m == method and (match := pattern.fullmatch(req.path)):
            if not public:
                _request.uid = request_user(req)
                if _request.uid is None:
                    if ON_VERCEL and not APP_PASSWORD and not MULTI_USER:
                        raise ApiError(503, "Set APP_PASSWORD (or ADMIN_EMAIL with Google sign-in) in the Vercel "
                                            "project's Environment Variables, then redeploy.")
                    if method == "GET" and not req.path.startswith("/api/"):
                        return redirect("/")
                    raise ApiError(401, "Please log in")
            return fn(req, *match.groups())
    if method == "GET":
        return serve_public(req.path)
    raise ApiError(404, "Not found")


def app(environ, start_response):
    """WSGI entry point (used by Vercel and by the local server)."""
    req = Request(environ)
    _request.uid = None  # threads are reused across requests
    client_day = req.header("X-Today")
    _request.today = client_day if re.fullmatch(r"\d{4}-\d{2}-\d{2}", client_day) else None
    try:
        status, headers, body = dispatch(req)
    except ApiError as e:
        status, headers, body = json_response({"error": e.message}, e.status)
    except (gdrive.DriveError, database.DatabaseError) as e:
        status, headers, body = json_response({"error": str(e)}, 502)
    except Exception as e:
        traceback.print_exc()
        status, headers, body = json_response({"error": f"Server error: {e}"}, 500)
    start_response(f"{status} {HTTPStatus(status).phrase}", headers)
    return [b"" if req.method == "HEAD" else body]


# ---------------------------------------------------------------- routes: session

def cookie(req, name, value, max_age):
    return ("Set-Cookie", f"{name}={value}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax"
                          + ("; Secure" if req.scheme == "https" else ""))


def session_cookie(req, uid):
    return cookie(req, SESSION_COOKIE, session_token(uid), SESSION_DAYS * 86400)


@route("POST", r"/api/login", public=True)
def login(req):
    secret = str(req.json().get("password", ""))
    # The browser extension connects with a code from the Resumes tab (a long-lived session token).
    if MULTI_USER and (uid := token_user(secret)) is not None and is_active(uid):
        return json_response({"token": secret})
    if not APP_PASSWORD:
        raise ApiError(400, "Sign in with Google instead" if MULTI_USER else "This tracker has no password set")
    if not hmac.compare_digest(secret.encode(), APP_PASSWORD.encode()):
        time.sleep(1)  # slows down password guessing
        raise ApiError(401, "Wrong password" + (" or connection code" if MULTI_USER else ""))
    token = session_token(ADMIN_ID)  # the password belongs to the admin
    return json_response({"token": token}, headers=[cookie(req, SESSION_COOKIE, token, SESSION_DAYS * 86400)])


@route("POST", r"/api/logout", public=True)
def logout(req):
    return json_response({"ok": True}, headers=[("Set-Cookie", f"{SESSION_COOKIE}=; Path=/; Max-Age=0"),
                                                ("Set-Cookie", f"{REQUEST_COOKIE}=; Path=/; Max-Age=0")])


# ---------------------------------------------------------------- routes: Google sign-in & access

def ensure_admin(db, name=""):
    """Create the admin's row as user 1 before anyone else, so nobody else can ever become user 1."""
    db.run_many([
        ("INSERT OR IGNORE INTO users (id, email, name, status, created_at) VALUES (?, ?, ?, 'active', ?)",
         (ADMIN_ID, ADMIN_EMAIL, name, now())),
        ("UPDATE users SET email = ?, status = 'active', name = CASE WHEN ? != '' THEN ? ELSE name END "
         "WHERE id = ?", (ADMIN_EMAIL, name, name, ADMIN_ID)),
    ])


def access_request(req):
    """The pending/declined request of someone who signed in but isn't let in yet."""
    token = req.cookie(REQUEST_COOKIE)
    if not MULTI_USER or not verify(token, "request"):
        return None
    uid = int(token.rpartition(".")[0].split(":")[2])
    rows = store()[0].query("SELECT id, email, name, status, note FROM users WHERE id = ?", (uid,))
    return rows[0] if rows and rows[0]["status"] != "active" else None


@route("GET", r"/login/google", public=True)
def google_login(req):
    if not MULTI_USER:
        raise ApiError(404, "Google sign-in isn't set up on this tracker")
    # The nonce cookie ties the sign-in to this browser, so nobody can sign you into their account
    # by sending you a link to the end of their own sign-in.
    nonce = secrets.token_hex(16)
    state = sign(f"login:{int(time.time()) + 900}:{nonce}")
    return response(b"", "text/plain", 302, [("Location", gdrive.login_url(redirect_uri(req), state)),
                                             cookie(req, LOGIN_COOKIE, nonce, 900)])


def finish_google_login(req):
    if "error" in req.query:  # e.g. they closed Google's account chooser
        return redirect("/")
    state = req.query.get("state", "")
    if not verify(state, "login") or not hmac.compare_digest(state.rpartition(".")[0].split(":")[2],
                                                             req.cookie(LOGIN_COOKIE) or "-"):
        raise ApiError(400, "Sign-in expired or didn't match. Go back and try again.")
    try:
        person = gdrive.signed_in_user(req.query.get("code", ""), redirect_uri(req))
    except gdrive.DriveError as e:
        return redirect(f"/?login_error={urllib.parse.quote(str(e)[:200])}")
    db, _ = store()
    ensure_admin(db)
    if person["email"] == ADMIN_EMAIL:
        ensure_admin(db, person["name"])
        uid = ADMIN_ID
    else:
        rows = db.query("SELECT id, status FROM users WHERE email = ?", (person["email"],))
        uid = rows[0]["id"] if rows else db.execute(
            "INSERT INTO users (email, name, status, created_at) VALUES (?, ?, 'pending', ?)",
            (person["email"], person["name"], now()))
        if rows and person["name"]:
            db.execute("UPDATE users SET name = ? WHERE id = ? AND name = ''", (person["name"], uid))
        if not rows or rows[0]["status"] != "active":
            token = sign(f"request:{int(time.time()) + 30 * 86400}:{uid}")
            return response(b"", "text/plain", 302, [("Location", "/"), cookie(req, REQUEST_COOKIE, token, 30 * 86400),
                                                     ("Set-Cookie", f"{SESSION_COOKIE}=; Path=/; Max-Age=0")])
    db.execute("UPDATE users SET last_login = ? WHERE id = ?", (now(), uid))
    return response(b"", "text/plain", 302, [("Location", "/"), session_cookie(req, uid),
                                             ("Set-Cookie", f"{REQUEST_COOKIE}=; Path=/; Max-Age=0")])


@route("POST", r"/api/access/note", public=True)
def access_note(req):
    request = access_request(req)
    if not request or request["status"] != "pending":
        raise ApiError(403, "No access request to update")
    note = str(req.json().get("note", "")).strip()[:500]
    store()[0].execute("UPDATE users SET note = ? WHERE id = ? AND status = 'pending'", (note, request["id"]))
    return json_response({"ok": True})


@route("GET", r"/api/admin/users")
def admin_users(req):
    require_admin()
    db, _ = store()
    ensure_admin(db)
    return json_response(db.query("""
        SELECT u.id, u.email, u.name, u.status, u.note, u.created_at, u.last_login,
               (SELECT count(*) FROM jobs j WHERE j.user_id = u.id) AS jobs,
               (SELECT coalesce(sum(r.size), 0) FROM resumes r WHERE r.user_id = u.id) AS resume_bytes
        FROM users u ORDER BY u.status = 'pending' DESC, u.created_at DESC"""))


@route("POST", r"/api/admin/users")
def admin_invite(req):
    """Invite someone: they get straight in the first time they sign in with this Google account."""
    require_admin()
    email = str(req.json().get("email", "")).strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ApiError(400, "That doesn't look like an email address")
    db, _ = store()
    ensure_admin(db)
    db.execute("INSERT INTO users (email, status, created_at) VALUES (?, 'active', ?) "
               "ON CONFLICT(email) DO UPDATE SET status = 'active'", (email, now()))
    return json_response({"ok": True}, 201)


@route("PUT", r"/api/admin/users/(\d+)")
def admin_set_status(req, uid):
    require_admin()
    status = req.json().get("status")
    if status not in ("active", "blocked"):
        raise ApiError(400, "status must be active or blocked")
    if int(uid) == ADMIN_ID:
        raise ApiError(400, "You can't change your own access")
    store()[0].execute("UPDATE users SET status = ? WHERE id = ?", (status, uid))
    return json_response({"ok": True})


@route("DELETE", r"/api/admin/users/(\d+)")
def admin_delete_user(req, uid):
    """Delete someone and everything they stored. Signing in again starts a new request."""
    require_admin()
    uid = int(uid)
    if uid == ADMIN_ID:
        raise ApiError(400, "You can't delete yourself")
    db, files = store()
    for r in db.query("SELECT id, filename FROM resumes WHERE user_id = ?", (uid,)):
        files.delete(r["id"], r["filename"])
    jobs = "SELECT id FROM jobs WHERE user_id = ?"
    db.run_many([
        (f"DELETE FROM events WHERE job_id IN ({jobs})", (uid,)),
        (f"DELETE FROM chat_messages WHERE job_id IN ({jobs})", (uid,)),
        ("DELETE FROM resume_reviews WHERE generated_id IN (SELECT id FROM generated_resumes WHERE user_id = ?)",
         (uid,)),
        ("DELETE FROM generated_resumes WHERE user_id = ?", (uid,)),
        ("DELETE FROM jobs WHERE user_id = ?", (uid,)),
        ("DELETE FROM resumes WHERE user_id = ?", (uid,)),
        ("DELETE FROM user_settings WHERE user_id = ?", (uid,)),
        ("DELETE FROM users WHERE id = ?", (uid,)),
    ])
    return json_response({"ok": True})


@route("POST", r"/api/extension-code")
def extension_code(req):
    """A connection code for the browser extension (Google sign-in can't run inside it)."""
    return json_response({"code": session_token(_request.uid, days=365)})


@route("GET", r"/api/meta", public=True)
def meta(req):
    _request.uid = request_user(req)
    auth = {"required": bool(APP_PASSWORD) or ON_VERCEL or MULTI_USER, "ok": _request.uid is not None,
            "google": MULTI_USER, "password": bool(APP_PASSWORD)}
    if not auth["ok"]:
        return json_response({"auth": auth, "statuses": STATUSES, "request": access_request(req)})
    db, files = backend()
    user = None
    if MULTI_USER:
        rows = db.query("SELECT email, name FROM users WHERE id = ?", (db.uid,))
        user = {**(rows[0] if rows else {"email": ADMIN_EMAIL, "name": ""}), "is_admin": is_admin()}
        if is_admin():
            user["pending"] = db.query("SELECT count(*) AS n FROM users WHERE status = 'pending'")[0]["n"]
    return json_response({
        "auth": auth,
        "user": user,
        "statuses": STATUSES,
        "origin": req.origin,
        "storage": files.kind,
        "resume_dir": str(files.folder) if files.kind == "folder" and db.uid == ADMIN_ID else None,
        "drive": {"configured": gdrive.is_configured(), "connected": gdrive.is_connected(db),
                  "folder_link": gdrive.cached_folder_link(db)},
    })


# ---------------------------------------------------------------- routes: jobs

@route("GET", r"/api/jobs")
def jobs_list(req):
    db, _ = backend()
    return json_response(db.query(JOB_LIST_SQL + " ORDER BY j.updated_at DESC", (db.uid,)))


@route("GET", r"/api/jobs/lookup")
def jobs_lookup(req):
    db, _ = backend()
    url = normalize_url(req.query.get("url", ""))
    rows = db.query(JOB_LIST_SQL + " AND j.url = ? AND j.url != ''", (db.uid, url))
    return json_response({"job": rows[0] if rows else None})


@route("GET", r"/api/jobs/(\d+)")
def jobs_get(req, job_id):
    db, _ = backend()
    job = find_job(db, job_id)
    events = db.query("SELECT at, kind, detail FROM events WHERE job_id = ? ORDER BY at DESC, id DESC", (job_id,))
    return json_response({**job, "events": events})


@route("POST", r"/api/jobs")
def jobs_create(req):
    db, _ = backend()
    job_id = insert_job(db, clean_job(req.json()))
    return json_response(job_summary(db, job_id), 201)


@route("POST", r"/api/jobs/capture")
def jobs_capture(req):
    """Used by the browser extension: create, or merge into the job with the same URL."""
    db, _ = backend()
    fields = clean_job(req.json())
    rows = db.query("SELECT * FROM jobs WHERE user_id = ? AND url = ? AND url != ''", (db.uid, fields.get("url", "")))
    if not rows:
        job_id = insert_job(db, fields)
        return json_response({"created": True, "job": job_summary(db, job_id)}, 201)
    existing = rows[0]
    merge = {}
    for key, value in fields.items():
        if value in ("", None):
            continue
        if key == "status":
            if existing["status"] == "saved":  # never move a job backwards
                merge[key] = value
        elif key == "notes" and existing["notes"]:
            if value not in existing["notes"]:
                merge[key] = existing["notes"] + "\n\n" + value
        elif key == "resume_id" or not existing[key]:
            merge[key] = value
    update_job(db, existing["id"], merge)
    return json_response({"created": False, "job": job_summary(db, existing["id"])})


@route("PUT", r"/api/jobs/(\d+)")
def jobs_update(req, job_id):
    db, _ = backend()
    update_job(db, int(job_id), clean_job(req.json()))
    return json_response(job_summary(db, job_id))


@route("DELETE", r"/api/jobs/(\d+)")
def jobs_delete(req, job_id):
    db, _ = backend()
    find_job(db, job_id)
    db.run_many([("DELETE FROM events WHERE job_id = ?", (job_id,)),
                 ("DELETE FROM chat_messages WHERE job_id = ?", (job_id,)),
                 ("DELETE FROM resume_reviews WHERE generated_id IN "
                  "(SELECT id FROM generated_resumes WHERE job_id = ?)", (job_id,)),
                 ("DELETE FROM generated_resumes WHERE job_id = ?", (job_id,)),
                 ("DELETE FROM jobs WHERE id = ? AND user_id = ?", (job_id, db.uid))])
    return json_response({"ok": True})


@route("POST", r"/api/import")
def import_url(req):
    return json_response(fetch_job(str(req.json().get("url", "")).strip()))


@route("GET", r"/api/export\.csv")
def export_csv(req):
    db, _ = backend()
    rows = db.query("""
        SELECT j.company, j.title, j.status, j.date_applied, j.follow_up, j.location, j.salary,
               j.source, j.url, r.label AS resume, j.notes, j.created_at, j.updated_at
        FROM jobs j LEFT JOIN resumes r ON r.id = j.resume_id AND r.user_id = j.user_id
        WHERE j.user_id = ? ORDER BY j.created_at""", (db.uid,))
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(rows[0].keys() if rows else ["company", "title", "status"])
    writer.writerows(r.values() for r in rows)
    return response(buf.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8", headers=[
        ("Content-Disposition", f"attachment; filename=job-applications-{today()}.csv")])


# ---------------------------------------------------------------- routes: resumes

@route("GET", r"/api/resumes")
def resumes_list(req):
    return json_response(list_resumes(*backend()))


@route("POST", r"/api/resumes")
def resumes_upload(req):
    db, files = backend()
    original = urllib.parse.unquote(req.header("X-Filename"))
    if not original:
        raise ApiError(400, "Missing X-Filename header")
    content = req.body(MAX_RESUME_BYTES)
    if not content:
        raise ApiError(400, "Empty file")
    filename = safe_filename(original, files)
    label = urllib.parse.unquote(req.header("X-Label")).strip() or Path(filename).stem
    rid = db.execute("INSERT INTO resumes (user_id, label, filename, size, uploaded_at) VALUES (?, ?, ?, ?, ?)",
                     (db.uid, label, filename, len(content), now()))
    try:
        files.save(rid, filename, content)
    except Exception:
        db.execute("DELETE FROM resumes WHERE id = ?", (rid,))
        raise
    drive_error = None
    if gdrive.is_connected(db):
        try:
            push_to_drive(db, files, {"id": rid, "filename": filename})
        except (gdrive.DriveError, ApiError) as e:
            drive_error = str(e)
    return json_response({**list_resumes(db, files, rid), "drive_error": drive_error}, 201)


@route("GET", r"/files/resumes/(\d+)(?:/.*)?")
def resume_file(req, rid):
    db, files = backend()
    resume = find_resume(db, rid)
    content = files.read(resume["id"], resume["filename"])
    if content is None:
        raise ApiError(404, "File is missing")
    return file_response(content, resume["filename"], "attachment" if req.query.get("download") else "inline")


@route("PUT", r"/api/resumes/(\d+)")
def resumes_rename(req, rid):
    db, _ = backend()
    label = str(req.json().get("label", "")).strip()
    if not label:
        raise ApiError(400, "Label can't be empty")
    find_resume(db, rid)
    db.execute("UPDATE resumes SET label = ? WHERE id = ? AND user_id = ?", (label, rid, db.uid))
    return json_response({"ok": True})


@route("DELETE", r"/api/resumes/(\d+)")
def resumes_delete(req, rid):
    db, files = backend()
    resume = find_resume(db, rid)
    files.delete(resume["id"], resume["filename"])
    db.run_many([("UPDATE jobs SET resume_id = NULL WHERE resume_id = ? AND user_id = ?", (rid, db.uid)),
                 ("DELETE FROM resumes WHERE id = ? AND user_id = ?", (rid, db.uid))])
    return json_response({"ok": True})


@route("POST", r"/api/resumes/(\d+)/drive")
def resumes_to_drive(req, rid):
    db, files = backend()
    push_to_drive(db, files, find_resume(db, rid))
    return json_response(list_resumes(db, files, rid))


# ---------------------------------------------------------------- routes: Google Drive

def redirect_uri(req):
    return f"{req.origin}/oauth2callback"


@route("GET", r"/drive/connect")
def drive_connect(req):
    db, _ = backend()
    state = sign(f"oauth:{int(time.time()) + 900}:{db.uid}:{secrets.token_hex(8)}")
    return redirect(gdrive.auth_url(redirect_uri(req), state))


@route("GET", r"/oauth2callback", public=True)
def google_callback(req):
    # One redirect address for both "Sign in with Google" and connecting Drive; the state says which.
    if req.query.get("state", "").startswith("login:"):
        return finish_google_login(req)
    _request.uid = request_user(req)
    state = req.query.get("state", "")
    if _request.uid is None:
        return redirect("/")
    # The state names the user who started connecting, so nobody can attach their Drive to your account.
    if not verify(state, "oauth") or state.split(":")[2] != str(_request.uid):
        raise ApiError(400, "Google sign-in expired or didn't match — start again from the Resumes tab")
    if "error" in req.query:
        return redirect(f"/?drive_error={urllib.parse.quote(req.query['error'])}#resumes")
    db, _ = backend()
    gdrive.exchange_code(db, req.query.get("code", ""), redirect_uri(req))
    gdrive.folder(db)  # create the Drive folder up front
    return redirect("/?drive=connected#resumes")


@route("POST", r"/api/drive/sync")
def drive_sync(req):
    db, files = backend()
    uploaded, errors = 0, []
    for r in list_resumes(db, files):
        if r["drive_id"] or r["missing"]:
            continue
        try:
            push_to_drive(db, files, r)
            uploaded += 1
        except (gdrive.DriveError, ApiError) as e:
            errors.append(f"{r['filename']}: {e}")
    return json_response({"uploaded": uploaded, "errors": errors})


@route("POST", r"/api/drive/disconnect")
def drive_disconnect(req):
    gdrive.disconnect(backend()[0])
    return json_response({"ok": True})


# ---------------------------------------------------------------- routes: profile & AI assistant

def ai_config(db):
    raw = db.get_setting("ai_config")
    return json.loads(raw) if raw else {}


def public_ai_config(config):
    key = config.get("api_key") or ""
    return {"provider": config.get("provider", "anthropic"), "model": config.get("model", ""),
            "base_url": config.get("base_url", ""), "has_key": bool(key),
            "key_hint": f"…{key[-4:]}" if len(key) > 8 else ""}


@route("GET", r"/api/profile")
def profile_get(req):
    db, _ = backend()
    return json_response({"profile": db.get_setting("profile") or "",
                          "instructions": db.get_setting("ai_instructions") or ""})


@route("PUT", r"/api/profile")
def profile_put(req):
    db, _ = backend()
    data = req.json()
    if "profile" in data:
        db.set_setting("profile", str(data["profile"]))
    if "instructions" in data:
        db.set_setting("ai_instructions", str(data["instructions"]))
    return json_response({"ok": True})


@route("GET", r"/api/ai")
def ai_get(req):
    db, _ = backend()
    providers = {k: {"label": v["label"], "base_url": v["base_url"], "model": v["model"]}
                 for k, v in ai.PROVIDERS.items()}
    return json_response({"config": public_ai_config(ai_config(db)), "providers": providers})


@route("PUT", r"/api/ai")
def ai_put(req):
    db, _ = backend()
    data = req.json()
    provider = str(data.get("provider", "anthropic"))
    if provider not in ai.PROVIDERS:
        raise ApiError(400, f"Unknown provider '{provider}'")
    old = ai_config(db)
    config = {
        "provider": provider,
        "model": str(data.get("model", "")).strip() or ai.PROVIDERS[provider]["model"],
        "base_url": str(data.get("base_url", "")).strip(),
        # A blank key means "keep the saved one" (the browser never gets the key back).
        "api_key": str(data.get("api_key", "")).strip() or old.get("api_key", ""),
    }
    db.set_setting("ai_config", json.dumps(config))
    return json_response({"config": public_ai_config(config)})


@route("GET", r"/api/ai/models")
def ai_models(req):
    db, _ = backend()
    try:
        return json_response(ai.list_models(ai_config(db)))
    except ai.AIError as e:
        raise ApiError(400, str(e)) from None


@route("POST", r"/api/ai/test")
def ai_test(req):
    db, _ = backend()
    try:
        reply = ai.complete(ai_config(db), "You are a connection test.",
                            [{"role": "user", "content": "Reply with just the word OK."}], max_tokens=1024)
    except ai.AIError as e:
        raise ApiError(400, str(e)) from None
    return json_response({"reply": reply[:200]})


def assistant_prompt(db, job):
    resume = db.query("SELECT label FROM resumes WHERE id = ? AND user_id = ?",
                      (job["resume_id"], db.uid)) if job["resume_id"] else []
    return ai.system_prompt(db.get_setting("profile") or "", db.get_setting("ai_instructions") or "",
                            job, resume[0]["label"] if resume else "")


@route("GET", r"/api/jobs/(\d+)/chat")
def chat_history(req, job_id):
    db, _ = backend()
    find_job(db, job_id)
    return json_response(db.query("SELECT id, role, content, created_at FROM chat_messages "
                                  "WHERE job_id = ? ORDER BY id", (job_id,)))


@route("POST", r"/api/jobs/(\d+)/chat")
def chat_send(req, job_id):
    db, _ = backend()
    job = find_job(db, job_id)
    text = str(req.json().get("message", "")).strip()
    if not text:
        raise ApiError(400, "Type a message first")
    history = db.query("SELECT role, content FROM chat_messages WHERE job_id = ? ORDER BY id", (job_id,))[-20:]
    while history and history[0]["role"] != "user":  # conversations must open with the user
        history.pop(0)
    try:
        reply = ai.complete(ai_config(db), assistant_prompt(db, job), [*history, {"role": "user", "content": text}])
    except ai.AIError as e:
        raise ApiError(400, str(e)) from None
    at = now()
    db.run_many([
        ("INSERT INTO chat_messages (job_id, role, content, created_at) VALUES (?, 'user', ?, ?)", (job_id, text, at)),
        ("INSERT INTO chat_messages (job_id, role, content, created_at) VALUES (?, 'assistant', ?, ?)", (job_id, reply, now())),
    ])
    return json_response({"user": {"role": "user", "content": text, "created_at": at},
                          "assistant": {"role": "assistant", "content": reply, "created_at": now()}})


@route("DELETE", r"/api/jobs/(\d+)/chat")
def chat_clear(req, job_id):
    db, _ = backend()
    find_job(db, job_id)
    db.execute("DELETE FROM chat_messages WHERE job_id = ?", (job_id,))
    return json_response({"ok": True})


def save_generated(db, job_id, content, title="", source="app"):
    job = find_job(db, job_id) if job_id else None
    content = str(content).strip()
    if not content:
        raise ApiError(400, "The resume is empty")
    if not title:
        name = re.match(r"#\s+(.+)", content)
        parts = [name.group(1).strip() if name else "Resume", job["company"] if job else "", today()]
        title = " - ".join(p for p in parts if p)
    return db.execute("INSERT INTO generated_resumes (user_id, job_id, title, content, source, created_at) "
                      "VALUES (?, ?, ?, ?, ?, ?)", (db.uid, job_id, title, content, source, now()))


@route("GET", r"/api/generated")
def generated_list(req):
    db, _ = backend()
    job_id = req.query.get("job_id")
    sql = ("SELECT g.id, g.job_id, g.title, g.source, g.created_at, j.company, j.title AS job_title "
           "FROM generated_resumes g LEFT JOIN jobs j ON j.id = g.job_id AND j.user_id = g.user_id "
           "WHERE g.user_id = ?")
    if not job_id:
        return json_response(db.query(sql + " ORDER BY g.id DESC", (db.uid,)))
    rows = db.query(sql.replace("g.created_at,", "g.created_at, g.content,") + " AND g.job_id = ? ORDER BY g.id DESC",
                    (db.uid, job_id))
    for r in rows:  # one job's few resumes: cheap enough to score on the fly
        r["score"] = check_resume(db, r)["score"]
        del r["content"]
    return json_response(rows)


@route("POST", r"/api/jobs/(\d+)/generated")
def generated_create(req, job_id):
    db, _ = backend()
    data = req.json()
    gid = save_generated(db, int(job_id), data.get("content", ""), str(data.get("title", "")).strip())
    return json_response({"id": gid, "url": f"/resume/{gid}"}, 201)


@route("GET", r"/api/generated/(\d+)")
def generated_get(req, gid):
    db, _ = backend()
    return json_response(find_generated(db, gid))


@route("PUT", r"/api/generated/(\d+)")
def generated_update(req, gid):
    db, _ = backend()
    data = req.json()
    find_generated(db, gid)
    db.execute("UPDATE generated_resumes SET content = ? WHERE id = ? AND user_id = ?",
               (str(data.get("content", "")), gid, db.uid))
    return json_response({"ok": True})


@route("DELETE", r"/api/generated/(\d+)")
def generated_delete(req, gid):
    db, _ = backend()
    find_generated(db, gid)
    db.run_many([("DELETE FROM resume_reviews WHERE generated_id = ?", (gid,)),
                 ("DELETE FROM generated_resumes WHERE id = ? AND user_id = ?", (gid, db.uid))])
    return json_response({"ok": True})


def find_generated(db, gid):
    rows = db.query("SELECT * FROM generated_resumes WHERE id = ? AND user_id = ?", (gid, db.uid))
    if not rows:
        raise ApiError(404, "Resume not found")
    return rows[0]


def content_hash(content):
    return hashlib.sha256(content.encode()).hexdigest()


def check_resume(db, doc):
    """Keyword and format scores for a tailored resume (no AI, so it's instant and free)."""
    job = db.query("SELECT id, company, title, description FROM jobs WHERE id = ? AND user_id = ?",
                   (doc["job_id"], db.uid)) \
        if doc["job_id"] else []
    job = job[0] if job else {}
    return {**scoring.score_resume(doc["content"], job.get("description", ""), db.get_setting("profile") or "",
                                   job.get("company", "")),
            "job_id": job.get("id"), "has_description": bool((job.get("description") or "").strip())}


def saved_review(db, doc):
    rows = db.query("SELECT * FROM resume_reviews WHERE generated_id = ?", (doc["id"],))
    if not rows:
        return None
    return {**json.loads(rows[0]["result"]), "created_at": rows[0]["created_at"],
            "stale": rows[0]["content_hash"] != content_hash(doc["content"])}


@route("GET", r"/api/generated/(\d+)/check")
def generated_check(req, gid):
    db, _ = backend()
    doc = find_generated(db, gid)
    return json_response({**check_resume(db, doc), "review": saved_review(db, doc),
                          "has_ai": bool(ai_config(db).get("api_key"))})


@route("POST", r"/api/generated/(\d+)/review")
def generated_review(req, gid):
    db, _ = backend()
    doc = find_generated(db, gid)
    job = db.query("SELECT * FROM jobs WHERE id = ? AND user_id = ?", (doc["job_id"], db.uid)) if doc["job_id"] else []
    try:
        result = ai.review_resume(ai_config(db), db.get_setting("profile") or "", job[0] if job else {},
                                  doc["content"])
    except ai.AIError as e:
        raise ApiError(400, str(e)) from None
    db.execute("INSERT INTO resume_reviews (generated_id, content_hash, result, created_at) VALUES (?, ?, ?, ?) "
               "ON CONFLICT(generated_id) DO UPDATE SET content_hash = excluded.content_hash, "
               "result = excluded.result, created_at = excluded.created_at",
               (doc["id"], content_hash(doc["content"]), json.dumps(result), now()))
    return json_response(saved_review(db, doc))


@route("GET", r"/resume/(\d+)", public=True)
def generated_page(req, gid):
    # Kept outside public/: Vercel serves public/ from its CDN and leaves it out of the
    # Python bundle. The page holds no data itself; it loads the resume with your login.
    return response((ROOT / "templates" / "print.html").read_bytes(), "text/html; charset=utf-8")


# ---------------------------------------------------------------- MCP (for Claude)

def mcp_token(db, rotate=False):
    token = None if rotate else db.get_setting("mcp_token")
    if not token:
        token = secrets.token_urlsafe(32)
        db.set_setting("mcp_token", token)
    return token


@route("GET", r"/api/mcp")
def mcp_info(req):
    db, _ = backend()
    return json_response({"url": f"{req.origin}/mcp", "token": mcp_token(db)})


@route("POST", r"/api/mcp/rotate")
def mcp_rotate(req):
    db, _ = backend()
    return json_response({"url": f"{req.origin}/mcp", "token": mcp_token(db, rotate=True)})


def mcp_tools(db, origin):
    def get_profile():
        return {"profile": db.get_setting("profile") or "(empty — ask the user to fill in the Profile tab)",
                "instructions": db.get_setting("ai_instructions") or ""}

    def list_jobs(status=None, query=None, limit=50):
        rows = db.query(JOB_LIST_SQL + " ORDER BY j.updated_at DESC", (db.uid,))
        if status:
            rows = [r for r in rows if r["status"] == status]
        if query:
            words = query.lower().split()
            rows = [r for r in rows if all(w in f"{r['company']} {r['title']} {r['location']} {r['notes']}".lower()
                                           for w in words)]
        keys = ("id", "company", "title", "status", "location", "date_applied", "resume_label", "url")
        return [{k: r[k] for k in keys} for r in rows[:max(1, min(int(limit), 200))]]

    def get_job(job_id):
        rows = db.query("SELECT * FROM jobs WHERE id = ? AND user_id = ?", (job_id, db.uid))
        if not rows:
            raise mcp.ToolError(f"No job with id {job_id}")
        job = rows[0]
        resume = db.query("SELECT label FROM resumes WHERE id = ? AND user_id = ?",
                          (job["resume_id"], db.uid)) if job["resume_id"] else []
        job["resume_sent"] = resume[0]["label"] if resume else None
        job["tailored_resumes"] = db.query("SELECT id, title, created_at FROM generated_resumes "
                                           "WHERE job_id = ? AND user_id = ?", (job_id, db.uid))
        job["history"] = db.query("SELECT at, kind, detail FROM events WHERE job_id = ? ORDER BY id", (job_id,))
        return job

    def list_resume_files():
        return [{k: r[k] for k in ("id", "label", "filename", "uploaded_at", "job_count")}
                for r in list_resumes(*backend())]

    def list_tailored_resumes(job_id=None):
        sql = "SELECT id, job_id, title, source, created_at FROM generated_resumes WHERE user_id = ?"
        return db.query(sql + " AND job_id = ? ORDER BY id DESC", (db.uid, job_id)) if job_id \
            else db.query(sql + " ORDER BY id DESC LIMIT 100", (db.uid,))

    def get_tailored_resume(resume_id):
        rows = db.query("SELECT * FROM generated_resumes WHERE id = ? AND user_id = ?", (resume_id, db.uid))
        if not rows:
            raise mcp.ToolError(f"No tailored resume with id {resume_id}")
        return rows[0]

    def check_summary(gid):
        report = check_resume(db, find_generated(db, gid))
        kw, label = report["keywords"], lambda ks: [k["label"] for k in ks]
        return {
            "score": report["score"],
            "keyword_match": kw and {"score": kw["score"], "matched": label(kw["matched"]),
                                     "missing_but_in_profile": label(kw["in_profile"]),
                                     "missing_not_in_profile": label(kw["gaps"])},
            "format": {"score": report["format"]["score"],
                       "issues": [f"{c['label']}: {c['detail']}" for c in report["format"]["checks"]
                                  if c["status"] != "pass"]},
        }

    def save_tailored_resume(job_id, content, title=""):
        try:
            gid = save_generated(db, int(job_id), content, title, source="claude")
        except ApiError as e:
            raise mcp.ToolError(e.message) from None
        return {"id": gid, "open_to_print_as_pdf": f"{origin}/resume/{gid}", "check": check_summary(gid)}

    def check_tailored_resume(resume_id):
        try:
            return check_summary(resume_id)
        except ApiError as e:
            raise mcp.ToolError(e.message) from None

    def update_tailored_resume(resume_id, content):
        if not db.query("SELECT 1 FROM generated_resumes WHERE id = ? AND user_id = ?", (resume_id, db.uid)):
            raise mcp.ToolError(f"No tailored resume with id {resume_id}")
        if not str(content).strip():
            raise mcp.ToolError("content is empty")
        db.execute("UPDATE generated_resumes SET content = ? WHERE id = ? AND user_id = ?",
                   (str(content).strip(), resume_id, db.uid))
        return {"id": resume_id, "open_to_print_as_pdf": f"{origin}/resume/{resume_id}",
                "check": check_summary(resume_id)}

    def change_job(job_id, status=None, note=None):
        rows = db.query("SELECT * FROM jobs WHERE id = ? AND user_id = ?", (job_id, db.uid))
        if not rows:
            raise mcp.ToolError(f"No job with id {job_id}")
        fields = {}
        if status:
            if status not in STATUSES:
                raise mcp.ToolError(f"status must be one of {', '.join(STATUSES)}")
            fields["status"] = status
        if note:
            fields["notes"] = (rows[0]["notes"] + "\n\n" if rows[0]["notes"] else "") + note
        update_job(db, int(job_id), fields)
        return job_summary(db, job_id)

    int_id = {"type": "integer", "description": "Job id from list_jobs"}
    return [
        mcp.Tool("get_profile", "The candidate's master profile (full experience, skills, education) and their "
                 "standing instructions for tailoring. Read this before writing any resume or answer.",
                 {}, [], get_profile),
        mcp.Tool("list_jobs", "List tracked job applications, newest activity first.",
                 {"status": {"type": "string", "enum": STATUSES}, "query": {"type": "string",
                  "description": "Words to match in company, title, location or notes"},
                  "limit": {"type": "integer", "minimum": 1, "maximum": 200}}, [], list_jobs),
        mcp.Tool("get_job", "Full details for one job: the saved job description, notes, which resume was sent, "
                 "status history, and tailored resumes already made for it.", {"job_id": int_id}, ["job_id"], get_job),
        mcp.Tool("list_resume_files", "Resume files the candidate has uploaded (names only; they are PDFs).",
                 {}, [], list_resume_files),
        mcp.Tool("list_tailored_resumes", "Tailored resumes saved in the tracker (Markdown), optionally for one job.",
                 {"job_id": int_id}, [], list_tailored_resumes),
        mcp.Tool("get_tailored_resume", "Read a saved tailored resume's full Markdown.",
                 {"resume_id": {"type": "integer"}}, ["resume_id"], get_tailored_resume),
        mcp.Tool("save_tailored_resume", "Save a tailored resume for a job so the candidate can open it in the "
                 "tracker and print it to PDF. content must follow this format so it lays out correctly:\n\n"
                 + ai.RESUME_FORMAT,
                 {"job_id": int_id, "content": {"type": "string"}, "title": {"type": "string"}},
                 ["job_id", "content"], save_tailored_resume),
        mcp.Tool("check_tailored_resume", "Score a saved tailored resume: keyword match against the job "
                 "description (matched, missing-but-in-profile, missing-and-not-in-profile) and format checks. "
                 "Add missing keywords only when the profile supports them; never add the ones not in the profile.",
                 {"resume_id": {"type": "integer"}}, ["resume_id"], check_tailored_resume),
        mcp.Tool("update_tailored_resume", "Replace a saved tailored resume's Markdown (same format as "
                 "save_tailored_resume), e.g. after acting on check_tailored_resume. Returns the new check.",
                 {"resume_id": {"type": "integer"}, "content": {"type": "string"}},
                 ["resume_id", "content"], update_tailored_resume),
        mcp.Tool("update_job", "Change a job's status and/or append a note.",
                 {"job_id": int_id, "status": {"type": "string", "enum": STATUSES}, "note": {"type": "string"}},
                 ["job_id"], change_job),
    ]


MCP_INSTRUCTIONS = ("Job Tracker holds the user's job applications, saved job descriptions, their master profile, "
                    "and tailored resumes. To tailor a resume: call get_profile and get_job, write the resume "
                    "using only facts from the profile, then save it with save_tailored_resume. Its result includes "
                    "a keyword and format check: if keywords the profile supports are missing, or format checks "
                    "fail, fix them with update_tailored_resume. Then give the user the returned link (they "
                    "print it to PDF from there).")


@route("POST", r"/mcp", public=True)
def mcp_endpoint(req):
    auth = req.header("Authorization")
    token = auth[7:] if auth.startswith("Bearer ") else req.query.get("key", "")
    rows = store()[0].query("SELECT user_id FROM user_settings WHERE key = 'mcp_token' AND value = ?",
                            (token,)) if token else []
    if not rows or (MULTI_USER and not is_active(rows[0]["user_id"])):
        time.sleep(1)
        raise ApiError(401, "Invalid MCP token — copy it from the Profile tab")
    _request.uid = rows[0]["user_id"]
    db, _ = backend()
    try:
        payload = json.loads(req.body() or b"null")
    except ValueError:
        return json_response({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
    tools = mcp_tools(db, req.origin)
    info = {"name": "job-tracker", "version": "1.0.0"}
    if isinstance(payload, list):
        replies = [r for r in (mcp.handle(m, tools, info, MCP_INSTRUCTIONS) for m in payload) if r]
    else:
        replies = mcp.handle(payload, tools, info, MCP_INSTRUCTIONS)
    if not replies:
        return response(b"", "text/plain", 202)
    return json_response(replies)


@route("GET", r"/mcp", public=True)
def mcp_get(req):
    # This server doesn't open server-to-client event streams.
    return response(b"", "text/plain", 405, [("Allow", "POST")])


# ---------------------------------------------------------------- local server

class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class QuietHandler(WSGIRequestHandler):
    def log_message(self, fmt, *args):
        if os.environ.get("JOB_TRACKER_DEBUG"):
            super().log_message(fmt, *args)


def main():
    store()
    server = make_server(HOST, PORT, app, server_class=ThreadingWSGIServer, handler_class=QuietHandler)
    url = f"http://{HOST}:{PORT}"
    print(f"Job Tracker running at {url}  (Ctrl+C to stop)")
    if "--open" in sys.argv:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
