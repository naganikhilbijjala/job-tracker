"""Multi-user mode: sign-in, access requests, and that nobody can reach anyone else's data.

Run with:  python3 -m unittest discover tests
Google is faked; everything else (routes, database, sessions) is the real app.
"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path

ADMIN = "admin@example.com"
os.environ.update({"ADMIN_EMAIL": ADMIN, "GOOGLE_CLIENT_ID": "test-id", "GOOGLE_CLIENT_SECRET": "test-secret"})
for var in ("APP_PASSWORD", "TURSO_DATABASE_URL", "VERCEL", "SESSION_SECRET"):
    os.environ.pop(var, None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import gdrive  # noqa: E402
import server  # noqa: E402

HOST = f"127.0.0.1:{server.PORT}"


class Client:
    """A browser: keeps cookies and talks to the WSGI app directly."""

    def __init__(self, bearer=None):
        self.cookies, self.bearer = {}, bearer

    def call(self, method, path, body=None, headers=None):
        path, _, query = path.partition("?")
        raw = body if isinstance(body, bytes) else json.dumps(body).encode() if body is not None else b""
        environ = {
            "REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": query, "HTTP_HOST": HOST,
            "wsgi.url_scheme": "http", "wsgi.input": io.BytesIO(raw), "CONTENT_LENGTH": str(len(raw)),
            "CONTENT_TYPE": "application/json",
            "HTTP_COOKIE": "; ".join(f"{k}={v}" for k, v in self.cookies.items()),
        }
        if self.bearer:
            environ["HTTP_AUTHORIZATION"] = f"Bearer {self.bearer}"
        for k, v in (headers or {}).items():
            environ["HTTP_" + k.upper().replace("-", "_")] = v
        out = {}

        def start_response(status, response_headers):
            out["status"], out["headers"] = int(status.split()[0]), response_headers

        content = b"".join(server.app(environ, start_response))
        for name, value in out["headers"]:
            if name == "Set-Cookie":
                key, _, rest = value.partition("=")
                val = rest.split(";")[0]
                if "Max-Age=0" in value:
                    self.cookies.pop(key, None)
                else:
                    self.cookies[key] = val
        out["headers"] = dict(out["headers"])
        ctype = out["headers"].get("Content-Type", "")
        out["json"] = json.loads(content) if "json" in ctype and content else None
        out["body"] = content
        return out

    def sign_in(self, email, name=""):
        gdrive.signed_in_user = lambda code, redirect_uri: {"email": email, "name": name}
        location = self.call("GET", "/login/google")["headers"]["Location"]
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)["state"][0]
        return self.call("GET", "/oauth2callback?" + urllib.parse.urlencode({"state": state, "code": "x"}))


class MultiUserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        server.DATA = Path(cls.tmp)
        server._backend = None
        assert server.MULTI_USER

        cls.admin = Client()
        cls.admin.sign_in(ADMIN, "Admin")
        cls.friend = Client()
        cls.friend.sign_in("friend@example.com", "Friend")  # asks for access
        users = cls.admin.call("GET", "/api/admin/users")["json"]
        friend_id = next(u["id"] for u in users if u["email"] == "friend@example.com")
        cls.admin.call("PUT", f"/api/admin/users/{friend_id}", {"status": "active"})
        cls.friend.sign_in("friend@example.com")
        cls.friend_id = friend_id

        # The admin's data: a job with a resume attached, a chat, a tailored resume and an AI review.
        a = cls.admin
        cls.a_resume = a.call("POST", "/api/resumes", b"%PDF admin secret resume",
                              {"X-Filename": "admin.pdf", "Content-Type": "application/pdf"})["json"]["id"]
        cls.a_job = a.call("POST", "/api/jobs", {"company": "SecretCo", "title": "Admin Job",
                                                 "url": "https://jobs.example.com/1", "description": "Python SQL",
                                                 "resume_id": cls.a_resume})["json"]["id"]
        cls.a_gen = a.call("POST", f"/api/jobs/{cls.a_job}/generated",
                           {"content": "# Admin\nadmin@example.com\n\n## Experience\n"})["json"]["id"]
        db, _ = server.store()
        db.run_many([("INSERT INTO chat_messages (job_id, role, content, created_at) VALUES (?, 'user', 'secret chat', '')",
                      (cls.a_job,)),
                     ("INSERT INTO resume_reviews (generated_id, content_hash, result, created_at) VALUES (?, 'x', ?, '')",
                      (cls.a_gen, json.dumps({"unsupported": [], "fit": 9, "verdict": "secret", "strengths": [],
                                              "weaknesses": []})))])
        a.call("PUT", "/api/profile", {"profile": "ADMIN PROFILE SECRET"})
        cls.a_mcp = a.call("GET", "/api/mcp")["json"]["token"]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def assert_admin_data_intact(self):
        a = self.admin
        job = a.call("GET", f"/api/jobs/{self.a_job}")["json"]
        self.assertEqual((job["company"], job["resume_id"]), ("SecretCo", self.a_resume))
        self.assertEqual(a.call("GET", f"/api/jobs/{self.a_job}/chat")["json"][0]["content"], "secret chat")
        self.assertIn("# Admin", a.call("GET", f"/api/generated/{self.a_gen}")["json"]["content"])
        self.assertEqual(a.call("GET", f"/files/resumes/{self.a_resume}")["body"], b"%PDF admin secret resume")
        self.assertEqual([r["label"] for r in a.call("GET", "/api/resumes")["json"]], ["admin"])
        self.assertEqual(a.call("GET", "/api/profile")["json"]["profile"], "ADMIN PROFILE SECRET")

    def test_cannot_reach_other_users_data(self):
        f, job, gen, res = self.friend, self.a_job, self.a_gen, self.a_resume
        not_found = [
            ("GET", f"/api/jobs/{job}", None), ("PUT", f"/api/jobs/{job}", {"notes": "hacked"}),
            ("DELETE", f"/api/jobs/{job}", None), ("GET", f"/api/jobs/{job}/chat", None),
            ("DELETE", f"/api/jobs/{job}/chat", None), ("POST", f"/api/jobs/{job}/chat", {"message": "hi"}),
            ("POST", f"/api/jobs/{job}/generated", {"content": "# x"}),
            ("GET", f"/api/generated/{gen}", None), ("PUT", f"/api/generated/{gen}", {"content": "hacked"}),
            ("DELETE", f"/api/generated/{gen}", None), ("GET", f"/api/generated/{gen}/check", None),
            ("POST", f"/api/generated/{gen}/review", None),
            ("GET", f"/files/resumes/{res}", None), ("PUT", f"/api/resumes/{res}", {"label": "hacked"}),
            ("DELETE", f"/api/resumes/{res}", None), ("POST", f"/api/resumes/{res}/drive", None),
            ("POST", "/api/jobs", {"company": "Mine", "resume_id": res}),
        ]
        for method, path, body in not_found:
            with self.subTest(route=f"{method} {path}"):
                self.assertEqual(f.call(method, path, body)["status"], 404)

        # Their own job can't point at someone else's resume either.
        own = f.call("POST", "/api/jobs", {"company": "FriendCo"})["json"]["id"]
        self.assertEqual(f.call("PUT", f"/api/jobs/{own}", {"resume_id": res})["status"], 404)

        # Lists, lookups, search and export only ever show their own things.
        jobs = f.call("GET", "/api/jobs")["json"]
        self.assertEqual([j["company"] for j in jobs], ["FriendCo"])
        self.assertIsNone(f.call("GET", "/api/jobs/lookup?url=https://jobs.example.com/1")["json"]["job"])
        self.assertEqual(f.call("GET", "/api/resumes")["json"], [])
        self.assertEqual(f.call("GET", "/api/generated")["json"], [])
        self.assertEqual(f.call("GET", f"/api/generated?job_id={job}")["json"], [])
        self.assertNotIn(b"SecretCo", f.call("GET", "/api/export.csv")["body"])
        self.assertEqual(f.call("GET", "/api/profile")["json"]["profile"], "")

        # Saving the same posting from the extension makes their own copy, not a merge into the admin's.
        captured = f.call("POST", "/api/jobs/capture", {"url": "https://jobs.example.com/1", "notes": "mine"})["json"]
        self.assertTrue(captured["created"])
        self.assertNotEqual(captured["job"]["id"], job)

        # Admin pages are off limits.
        for method, path, body in [("GET", "/api/admin/users", None), ("POST", "/api/admin/users", {"email": "x@y.z"}),
                                   ("PUT", "/api/admin/users/1", {"status": "blocked"}),
                                   ("DELETE", f"/api/admin/users/{self.friend_id}", None)]:
            with self.subTest(route=f"{method} {path}"):
                self.assertEqual(f.call(method, path, body)["status"], 403)

        self.assert_admin_data_intact()

    def test_mcp_tokens_are_per_user(self):
        friend_token = self.friend.call("GET", "/api/mcp")["json"]["token"]
        self.assertNotEqual(friend_token, self.a_mcp)

        def tool(token, name, args):
            res = Client(bearer=token).call("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                             "params": {"name": name, "arguments": args}})
            return res["json"]["result"]

        self.assertTrue(tool(friend_token, "get_job", {"job_id": self.a_job})["isError"])
        self.assertTrue(tool(friend_token, "get_tailored_resume", {"resume_id": self.a_gen})["isError"])
        self.assertTrue(tool(friend_token, "update_tailored_resume", {"resume_id": self.a_gen, "content": "x"})["isError"])
        self.assertTrue(tool(friend_token, "check_tailored_resume", {"resume_id": self.a_gen})["isError"])
        self.assertTrue(tool(friend_token, "update_job", {"job_id": self.a_job, "note": "x"})["isError"])
        self.assertNotIn("SecretCo", tool(friend_token, "list_jobs", {})["content"][0]["text"])
        self.assertNotIn("SECRET", tool(friend_token, "get_profile", {})["content"][0]["text"])
        self.assertIn("SecretCo", tool(self.a_mcp, "list_jobs", {})["content"][0]["text"])
        self.assertEqual(Client(bearer="wrong").call("POST", "/mcp", {"jsonrpc": "2.0", "id": 1,
                                                                      "method": "tools/list"})["status"], 401)
        self.assert_admin_data_intact()

    def test_access_requests(self):
        stranger = Client()
        stranger.sign_in("stranger@example.com", "Stranger")
        self.assertNotIn(server.SESSION_COOKIE, stranger.cookies)
        meta = stranger.call("GET", "/api/meta")["json"]
        self.assertFalse(meta["auth"]["ok"])
        self.assertEqual(meta["request"]["status"], "pending")
        self.assertEqual(stranger.call("GET", "/api/jobs")["status"], 401)
        self.assertEqual(stranger.call("POST", "/api/access/note", {"note": "Hi, it's Sam"})["status"], 200)

        users = self.admin.call("GET", "/api/admin/users")["json"]
        req = next(u for u in users if u["email"] == "stranger@example.com")
        self.assertEqual((req["status"], req["note"]), ("pending", "Hi, it's Sam"))
        self.assertEqual(self.admin.call("GET", "/api/meta")["json"]["user"]["pending"], 1)

        # Declined: signing in again still doesn't get them in.
        self.admin.call("PUT", f"/api/admin/users/{req['id']}", {"status": "blocked"})
        stranger.sign_in("stranger@example.com")
        self.assertEqual(stranger.call("GET", "/api/meta")["json"]["request"]["status"], "blocked")
        self.assertEqual(stranger.call("POST", "/api/access/note", {"note": "pls"})["status"], 403)

        # Invited people get straight in on their first sign-in.
        self.admin.call("POST", "/api/admin/users", {"email": "Invited@Example.com"})
        invited = Client()
        invited.sign_in("invited@example.com", "Invited")
        self.assertTrue(invited.call("GET", "/api/meta")["json"]["auth"]["ok"])

    def test_removing_access_logs_out_everywhere(self):
        c = Client()
        c.sign_in("temp@example.com")
        uid = next(u["id"] for u in self.admin.call("GET", "/api/admin/users")["json"] if u["email"] == "temp@example.com")
        self.admin.call("PUT", f"/api/admin/users/{uid}", {"status": "active"})
        c.sign_in("temp@example.com")
        code = c.call("POST", "/api/extension-code")["json"]["code"]
        extension = Client(bearer=Client().call("POST", "/api/login", {"password": code})["json"]["token"])
        c.call("POST", "/api/jobs", {"company": "TempCo"})
        self.assertEqual(extension.call("GET", "/api/jobs")["status"], 200)

        self.admin.call("PUT", f"/api/admin/users/{uid}", {"status": "blocked"})
        self.assertEqual(c.call("GET", "/api/jobs")["status"], 401)
        self.assertEqual(extension.call("GET", "/api/jobs")["status"], 401)

        # Deleting removes their data; the admin can't remove or delete themselves.
        self.assertEqual(self.admin.call("DELETE", f"/api/admin/users/{uid}")["status"], 200)
        db, _ = server.store()
        self.assertEqual(db.query("SELECT count(*) AS n FROM jobs WHERE user_id = ?", (uid,))[0]["n"], 0)
        self.assertEqual(self.admin.call("PUT", "/api/admin/users/1", {"status": "blocked"})["status"], 400)
        self.assertEqual(self.admin.call("DELETE", "/api/admin/users/1")["status"], 400)
        self.assert_admin_data_intact()

    def test_drive_connection_is_bound_to_who_started_it(self):
        location = self.friend.call("GET", "/drive/connect")["headers"]["Location"]
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)["state"][0]
        # The admin's browser finishing the friend's Drive sign-in must not attach the friend's Drive.
        res = self.admin.call("GET", "/oauth2callback?" + urllib.parse.urlencode({"state": state, "code": "x"}))
        self.assertEqual(res["status"], 400)

    def test_sign_in_is_tied_to_the_browser_that_started_it(self):
        attacker = Client()
        location = attacker.call("GET", "/login/google")["headers"]["Location"]
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(location).query)["state"][0]
        gdrive.signed_in_user = lambda code, redirect_uri: {"email": "friend@example.com", "name": ""}
        victim = Client()
        res = victim.call("GET", "/oauth2callback?" + urllib.parse.urlencode({"state": state, "code": "x"}))
        self.assertEqual(res["status"], 400)
        self.assertNotIn(server.SESSION_COOKIE, victim.cookies)

    def test_forged_or_anonymous_requests(self):
        self.assertEqual(Client().call("GET", "/api/jobs")["status"], 401)
        forged = Client()
        forged.cookies[server.SESSION_COOKIE] = "session:9999999999:1.0000000000000000000000000000000000000000"
        self.assertEqual(forged.call("GET", "/api/jobs")["status"], 401)


if __name__ == "__main__":
    unittest.main()
