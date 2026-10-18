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

ROOT = Path(__file__).resolve().parent
PUBLIC = ROOT / "public"
DATA = ROOT / "data"
HOST = "127.0.0.1"
PORT = int(os.environ.get("JOB_TRACKER_PORT", 8765))
LOCAL_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
ON_VERCEL = bool(os.environ.get("VERCEL"))
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
# Sessions are signed with a key derived from the password, so changing it logs everyone out.
SECRET = (hashlib.sha256(b"job-tracker:" + APP_PASSWORD.encode()).digest()
          if APP_PASSWORD else secrets.token_bytes(32))
SESSION_COOKIE = "jt_session"
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


def backend():
    """(db, files), opened on first use so a cold start only pays for it when needed."""
    global _backend
    with _backend_lock:
        if _backend is None:
            _backend = database.open_backend(DATA)
        return _backend


def log_event(job_id, kind, detail=""):
    return ("INSERT INTO events (job_id, at, kind, detail) VALUES (?, ?, ?, ?)", (job_id, now(), kind, detail))


JOB_LIST_SQL = """
SELECT j.id, j.company, j.title, j.location, j.url, j.source, j.salary, j.status,
       j.notes, j.resume_id, r.label AS resume_label, j.date_applied, j.follow_up,
       j.created_at, j.updated_at, length(j.description) AS desc_len
FROM jobs j LEFT JOIN resumes r ON r.id = j.resume_id
"""


def job_summary(db, job_id):
    rows = db.query(JOB_LIST_SQL + " WHERE j.id = ?", (job_id,))
    return rows[0] if rows else None


def find_job(db, job_id):
    rows = db.query("SELECT * FROM jobs WHERE id = ?", (job_id,))
    if not rows:
        raise ApiError(404, "Job not found")
    return rows[0]


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
    status_side_effects(fields, None)
    fields["created_at"] = fields["updated_at"] = now()
    cols = ", ".join(fields)
    marks = ", ".join("?" * len(fields))
    job_id = db.execute(f"INSERT INTO jobs ({cols}) VALUES ({marks})", list(fields.values()))
    db.execute(*log_event(job_id, "created", fields["status"]))
    return job_id


def update_job(db, job_id, fields):
    old = find_job(db, job_id)
    status_side_effects(fields, old)
    if not fields:
        return
    fields["updated_at"] = now()
    sets = ", ".join(f"{k} = ?" for k in fields)
    statements = [(f"UPDATE jobs SET {sets} WHERE id = ?", [*fields.values(), job_id])]
    if "status" in fields and fields["status"] != old["status"]:
        statements.append(log_event(job_id, "status", f"{old['status']} → {fields['status']}"))
    if fields.get("resume_id") and fields["resume_id"] != old["resume_id"]:
        statements.append(("INSERT INTO events (job_id, at, kind, detail) "
                           "SELECT ?, ?, 'resume', label FROM resumes WHERE id = ?",
                           (job_id, now(), fields["resume_id"])))
    db.run_many(statements)


def sync_resume_folder(db, files):
    """Pick up files dropped straight into data/resumes/ (local mode only)."""
    if files.kind != "folder":
        return
    known = {r["filename"] for r in db.query("SELECT filename FROM resumes")}
    for p in sorted(files.folder.iterdir()):
        if p.is_file() and p.suffix.lower() in RESUME_EXTS and p.name not in known:
            st = p.stat()
            db.execute("INSERT INTO resumes (label, filename, size, uploaded_at) VALUES (?, ?, ?, ?)",
                       (p.stem, p.name, st.st_size, datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")))


def list_resumes(db, files, only_id=None):
    sync_resume_folder(db, files)
    rows = db.query("""
        SELECT r.*, (SELECT count(*) FROM jobs j WHERE j.resume_id = r.id) AS job_count
        FROM resumes r ORDER BY r.uploaded_at DESC""")
    out = [{**r, "missing": files.missing(r)} for r in rows]
    if only_id is not None:
        return next((r for r in out if r["id"] == int(only_id)), None)
    return out


def find_resume(db, rid):
    rows = db.query("SELECT * FROM resumes WHERE id = ?", (rid,))
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
    db.execute("UPDATE resumes SET drive_id = ?, drive_link = ? WHERE id = ?", (file_id, link, resume["id"]))


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


def is_logged_in(req):
    if not APP_PASSWORD:
        return not ON_VERCEL  # a public deployment must never run without a password
    auth = req.header("Authorization")
    token = auth[7:] if auth.startswith("Bearer ") else req.cookie(SESSION_COOKIE)
    return verify(token, "session")


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
            if not public and not is_logged_in(req):
                if ON_VERCEL and not APP_PASSWORD:
                    raise ApiError(503, "Set APP_PASSWORD in the Vercel project's Environment Variables, then redeploy.")
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

@route("POST", r"/api/login", public=True)
def login(req):
    if not APP_PASSWORD:
        raise ApiError(400, "This tracker has no password set")
    if not hmac.compare_digest(str(req.json().get("password", "")).encode(), APP_PASSWORD.encode()):
        time.sleep(1)  # slows down password guessing
        raise ApiError(401, "Wrong password")
    token = sign(f"session:{int(time.time()) + SESSION_DAYS * 86400}")
    cookie = (f"{SESSION_COOKIE}={token}; Path=/; Max-Age={SESSION_DAYS * 86400}; HttpOnly; SameSite=Lax"
              + ("; Secure" if req.scheme == "https" else ""))
    return json_response({"token": token}, headers=[("Set-Cookie", cookie)])


@route("POST", r"/api/logout", public=True)
def logout(req):
    return json_response({"ok": True}, headers=[("Set-Cookie", f"{SESSION_COOKIE}=; Path=/; Max-Age=0")])


@route("GET", r"/api/meta", public=True)
def meta(req):
    auth = {"required": bool(APP_PASSWORD) or ON_VERCEL, "ok": is_logged_in(req)}
    if not auth["ok"]:
        return json_response({"auth": auth, "statuses": STATUSES})
    db, files = backend()
    return json_response({
        "auth": auth,
        "statuses": STATUSES,
        "origin": req.origin,
        "storage": files.kind,
        "resume_dir": str(files.folder) if files.kind == "folder" else None,
        "drive": {"configured": gdrive.is_configured(), "connected": gdrive.is_connected(db),
                  "folder_link": gdrive.cached_folder_link(db)},
    })


# ---------------------------------------------------------------- routes: jobs

@route("GET", r"/api/jobs")
def jobs_list(req):
    db, _ = backend()
    return json_response(db.query(JOB_LIST_SQL + " ORDER BY j.updated_at DESC"))


@route("GET", r"/api/jobs/lookup")
def jobs_lookup(req):
    db, _ = backend()
    url = normalize_url(req.query.get("url", ""))
    rows = db.query(JOB_LIST_SQL + " WHERE j.url = ? AND j.url != ''", (url,))
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
    rows = db.query("SELECT * FROM jobs WHERE url = ? AND url != ''", (fields.get("url", ""),))
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
    db.run_many([("DELETE FROM events WHERE job_id = ?", (job_id,)),
                 ("DELETE FROM chat_messages WHERE job_id = ?", (job_id,)),
                 ("DELETE FROM generated_resumes WHERE job_id = ?", (job_id,)),
                 ("DELETE FROM jobs WHERE id = ?", (job_id,))])
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
        FROM jobs j LEFT JOIN resumes r ON r.id = j.resume_id ORDER BY j.created_at""")
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
    rid = db.execute("INSERT INTO resumes (label, filename, size, uploaded_at) VALUES (?, ?, ?, ?)",
                     (label, filename, len(content), now()))
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
    db.execute("UPDATE resumes SET label = ? WHERE id = ?", (label, rid))
    return json_response({"ok": True})


@route("DELETE", r"/api/resumes/(\d+)")
def resumes_delete(req, rid):
    db, files = backend()
    resume = find_resume(db, rid)
    files.delete(resume["id"], resume["filename"])
    db.run_many([("UPDATE jobs SET resume_id = NULL WHERE resume_id = ?", (rid,)),
                 ("DELETE FROM resumes WHERE id = ?", (rid,))])
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
    state = sign(f"oauth:{int(time.time()) + 900}:{secrets.token_hex(8)}")
    return redirect(gdrive.auth_url(redirect_uri(req), state))


@route("GET", r"/oauth2callback")
def drive_callback(req):
    if not verify(req.query.get("state", ""), "oauth"):
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
    resume = db.query("SELECT label FROM resumes WHERE id = ?", (job["resume_id"],)) if job["resume_id"] else []
    return ai.system_prompt(db.get_setting("profile") or "", db.get_setting("ai_instructions") or "",
                            job, resume[0]["label"] if resume else "")


@route("GET", r"/api/jobs/(\d+)/chat")
def chat_history(req, job_id):
    db, _ = backend()
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
    return db.execute("INSERT INTO generated_resumes (job_id, title, content, source, created_at) "
                      "VALUES (?, ?, ?, ?, ?)", (job_id, title, content, source, now()))


@route("GET", r"/api/generated")
def generated_list(req):
    db, _ = backend()
    job_id = req.query.get("job_id")
    sql = ("SELECT g.id, g.job_id, g.title, g.source, g.created_at, j.company, j.title AS job_title "
           "FROM generated_resumes g LEFT JOIN jobs j ON j.id = g.job_id")
    rows = db.query(sql + " WHERE g.job_id = ? ORDER BY g.id DESC", (job_id,)) if job_id \
        else db.query(sql + " ORDER BY g.id DESC")
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
    rows = db.query("SELECT * FROM generated_resumes WHERE id = ?", (gid,))
    if not rows:
        raise ApiError(404, "Resume not found")
    return json_response(rows[0])


@route("PUT", r"/api/generated/(\d+)")
def generated_update(req, gid):
    db, _ = backend()
    data = req.json()
    db.execute("UPDATE generated_resumes SET content = ? WHERE id = ?", (str(data.get("content", "")), gid))
    return json_response({"ok": True})


@route("DELETE", r"/api/generated/(\d+)")
def generated_delete(req, gid):
    db, _ = backend()
    db.execute("DELETE FROM generated_resumes WHERE id = ?", (gid,))
    return json_response({"ok": True})


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
        rows = db.query(JOB_LIST_SQL + " ORDER BY j.updated_at DESC")
        if status:
            rows = [r for r in rows if r["status"] == status]
        if query:
            words = query.lower().split()
            rows = [r for r in rows if all(w in f"{r['company']} {r['title']} {r['location']} {r['notes']}".lower()
                                           for w in words)]
        keys = ("id", "company", "title", "status", "location", "date_applied", "resume_label", "url")
        return [{k: r[k] for k in keys} for r in rows[:max(1, min(int(limit), 200))]]

    def get_job(job_id):
        rows = db.query("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if not rows:
            raise mcp.ToolError(f"No job with id {job_id}")
        job = rows[0]
        resume = db.query("SELECT label FROM resumes WHERE id = ?", (job["resume_id"],)) if job["resume_id"] else []
        job["resume_sent"] = resume[0]["label"] if resume else None
        job["tailored_resumes"] = db.query("SELECT id, title, created_at FROM generated_resumes WHERE job_id = ?",
                                           (job_id,))
        job["history"] = db.query("SELECT at, kind, detail FROM events WHERE job_id = ? ORDER BY id", (job_id,))
        return job

    def list_resume_files():
        return [{k: r[k] for k in ("id", "label", "filename", "uploaded_at", "job_count")}
                for r in list_resumes(*backend())]

    def list_tailored_resumes(job_id=None):
        sql = "SELECT id, job_id, title, source, created_at FROM generated_resumes"
        return db.query(sql + " WHERE job_id = ? ORDER BY id DESC", (job_id,)) if job_id \
            else db.query(sql + " ORDER BY id DESC LIMIT 100")

    def get_tailored_resume(resume_id):
        rows = db.query("SELECT * FROM generated_resumes WHERE id = ?", (resume_id,))
        if not rows:
            raise mcp.ToolError(f"No tailored resume with id {resume_id}")
        return rows[0]

    def save_tailored_resume(job_id, content, title=""):
        try:
            gid = save_generated(db, int(job_id), content, title, source="claude")
        except ApiError as e:
            raise mcp.ToolError(e.message) from None
        return {"id": gid, "open_to_print_as_pdf": f"{origin}/resume/{gid}"}

    def change_job(job_id, status=None, note=None):
        rows = db.query("SELECT * FROM jobs WHERE id = ?", (job_id,))
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
                 "tracker and print it to PDF. content is Markdown: '# Full Name', a contact line, then '## ' "
                 "sections with '- ' bullets; single column, no tables.",
                 {"job_id": int_id, "content": {"type": "string"}, "title": {"type": "string"}},
                 ["job_id", "content"], save_tailored_resume),
        mcp.Tool("update_job", "Change a job's status and/or append a note.",
                 {"job_id": int_id, "status": {"type": "string", "enum": STATUSES}, "note": {"type": "string"}},
                 ["job_id"], change_job),
    ]


MCP_INSTRUCTIONS = ("Job Tracker holds the user's job applications, saved job descriptions, their master profile, "
                    "and tailored resumes. To tailor a resume: call get_profile and get_job, write the resume "
                    "using only facts from the profile, then save it with save_tailored_resume and give the user "
                    "the returned link (they print it to PDF from there).")


@route("POST", r"/mcp", public=True)
def mcp_endpoint(req):
    db, _ = backend()
    auth = req.header("Authorization")
    token = auth[7:] if auth.startswith("Bearer ") else req.query.get("key", "")
    expected = db.get_setting("mcp_token")
    if not expected or not hmac.compare_digest(token.encode(), expected.encode()):
        time.sleep(1)
        raise ApiError(401, "Invalid MCP token — copy it from the Profile tab")
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
    backend()
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
