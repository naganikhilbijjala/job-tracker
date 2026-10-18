"""Minimal Google client (stdlib only): "Sign in with Google", and Drive backup of resumes.

Uses the `drive.file` scope, so the app can only see files it created itself —
it never gets access to the rest of your Drive. Tokens are kept in the app's
settings table so this works on serverless hosts too.
"""
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CLIENT_FILE = Path(__file__).resolve().parent / "data" / "client_secret.json"
SCOPE = "https://www.googleapis.com/auth/drive.file"
LOGIN_SCOPE = "openid email profile"
FOLDER_NAME = "Job Tracker Resumes"

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/drive/v3/files"
UPLOAD_API = "https://www.googleapis.com/upload/drive/v3/files"
USERINFO = "https://openidconnect.googleapis.com/v1/userinfo"

_lock = threading.Lock()


class DriveError(Exception):
    pass


def _client():
    client_id, secret = os.environ.get("GOOGLE_CLIENT_ID"), os.environ.get("GOOGLE_CLIENT_SECRET")
    if client_id and secret:
        return client_id, secret
    try:
        data = json.loads(CLIENT_FILE.read_text())
    except FileNotFoundError:
        raise DriveError("Google Drive isn't set up yet — see the README.") from None
    c = data.get("installed") or data.get("web") or {}
    if "client_id" not in c:
        raise DriveError("data/client_secret.json doesn't look like a Google OAuth client file")
    return c["client_id"], c["client_secret"]


def client_secret():
    return _client()[1]


def is_configured():
    try:
        _client()
        return True
    except DriveError:
        return False


def is_connected(db):
    return bool(db.get_setting("google_token"))


def _open(req):
    try:
        with urllib.request.urlopen(req, timeout=60) as res:
            body = res.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        raise DriveError(f"Google API error {e.code}: {detail}") from None
    except urllib.error.URLError as e:
        raise DriveError(f"Couldn't reach Google: {e.reason}") from None


def _post_form(url, fields):
    return _open(urllib.request.Request(
        url, data=urllib.parse.urlencode(fields).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"}))


def auth_url(redirect_uri, state):
    client_id, _ = _client()
    return AUTH_URI + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
        "scope": SCOPE, "access_type": "offline", "prompt": "consent", "state": state,
    })


def login_url(redirect_uri, state):
    """Google's account chooser, asking only for name and email."""
    client_id, _ = _client()
    return AUTH_URI + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code",
        "scope": LOGIN_SCOPE, "prompt": "select_account", "state": state,
    })


def signed_in_user(code, redirect_uri):
    """Finish "Sign in with Google": returns {"email", "name"} for a verified Google account."""
    client_id, secret = _client()
    token = _post_form(TOKEN_URI, {
        "code": code, "client_id": client_id, "client_secret": secret,
        "redirect_uri": redirect_uri, "grant_type": "authorization_code",
    })
    info = _open(urllib.request.Request(USERINFO, headers={"Authorization": f"Bearer {token['access_token']}"}))
    if not info.get("email") or not info.get("email_verified"):
        raise DriveError("Google didn't confirm an email address for this account.")
    return {"email": info["email"].strip().lower(), "name": (info.get("name") or "").strip()}


def _save_token(db, token):
    token["expires_at"] = time.time() + token.get("expires_in", 3600) - 60
    db.set_setting("google_token", json.dumps(token))


def exchange_code(db, code, redirect_uri):
    client_id, secret = _client()
    _save_token(db, _post_form(TOKEN_URI, {
        "code": code, "client_id": client_id, "client_secret": secret,
        "redirect_uri": redirect_uri, "grant_type": "authorization_code",
    }))


def disconnect(db):
    db.set_setting("google_token", None)
    db.set_setting("google_folder", None)


def _access_token(db):
    with _lock:
        raw = db.get_setting("google_token")
        if not raw:
            raise DriveError("Not connected to Google Drive")
        token = json.loads(raw)
        if token.get("expires_at", 0) > time.time():
            return token["access_token"]
        if not token.get("refresh_token"):
            raise DriveError("Google Drive session expired — reconnect from the Resumes tab")
        client_id, secret = _client()
        token.update(_post_form(TOKEN_URI, {
            "client_id": client_id, "client_secret": secret,
            "refresh_token": token["refresh_token"], "grant_type": "refresh_token",
        }))
        _save_token(db, token)
        return token["access_token"]


def _api(db, method, url, body=None, content_type="application/json"):
    headers = {"Authorization": f"Bearer {_access_token(db)}"}
    if body is not None:
        if isinstance(body, dict):
            body = json.dumps(body).encode()
        headers["Content-Type"] = content_type
    return _open(urllib.request.Request(url, data=body, method=method, headers=headers))


def cached_folder_link(db):
    raw = db.get_setting("google_folder")
    return json.loads(raw).get("link") if raw else None


def folder(db, refresh=False):
    """Return (id, link) of the 'Job Tracker Resumes' folder, creating it if needed."""
    raw = db.get_setting("google_folder")
    if raw and not refresh:
        f = json.loads(raw)
        return f["id"], f.get("link")
    q = f"name = '{FOLDER_NAME}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    found = _api(db, "GET", API + "?" + urllib.parse.urlencode({"q": q, "fields": "files(id,webViewLink)"}))
    f = (found.get("files") or [None])[0] or _api(
        db, "POST", API + "?fields=id,webViewLink",
        {"name": FOLDER_NAME, "mimeType": "application/vnd.google-apps.folder"})
    db.set_setting("google_folder", json.dumps({"id": f["id"], "link": f.get("webViewLink")}))
    return f["id"], f.get("webViewLink")


def upload(db, content, name, mime):
    """Upload bytes into the resumes folder. Returns (file_id, web_link)."""
    for attempt in range(2):
        folder_id, _ = folder(db, refresh=attempt > 0)
        boundary = "jobtracker" + secrets.token_hex(8)
        meta = json.dumps({"name": name, "parents": [folder_id]})
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n"
                f"--{boundary}\r\nContent-Type: {mime}\r\n\r\n").encode() + content + \
               f"\r\n--{boundary}--\r\n".encode()
        try:
            res = _api(db, "POST", UPLOAD_API + "?uploadType=multipart&fields=id,webViewLink",
                       body, f"multipart/related; boundary={boundary}")
            return res["id"], res.get("webViewLink", "")
        except DriveError as e:
            # The cached folder may have been deleted in Drive — look it up again once.
            if attempt or "404" not in str(e):
                raise
