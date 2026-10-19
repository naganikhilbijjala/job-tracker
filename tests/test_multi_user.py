"""Multi-user mode: open sign-up, job limits, turning access off, and that nobody can reach anyone else's data.

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

import database  # noqa: E402
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
        cls.friend.sign_in("friend@example.com", "Friend")
        cls.friend_id = cls.user_id("friend@example.com")

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
    def user_id(cls, email):
        return next(u["id"] for u in cls.admin.call("GET", "/api/admin/users")["json"]["users"] if u["email"] == email)

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
            ("GET", f"/api/generated/{gen}/pdf", None),
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
        for method, path, body in [("GET", "/api/admin/users", None),
                                   ("PUT", "/api/admin/users/1", {"status": "blocked"}),
                                   ("PUT", f"/api/admin/users/{self.friend_id}", {"job_limit": 1000}),
                                   ("DELETE", f"/api/admin/users/{self.friend_id}", None)]:
            with self.subTest(route=f"{method} {path}"):
                self.assertEqual(f.call(method, path, body)["status"], 403)

        self.assert_admin_data_intact()

    def test_ai_keys_are_per_provider_and_per_user(self):
        f = self.friend
        f.call("PUT", "/api/ai", {"provider": "anthropic", "model": "claude-x", "api_key": "sk-ant-friend-1111"})
        f.call("PUT", "/api/ai", {"provider": "openai", "model": "gpt-x", "api_key": "sk-openai-friend-2222"})
        # Switching back with a blank key keeps the key saved for that provider.
        res = f.call("PUT", "/api/ai", {"provider": "anthropic", "model": "claude-x", "api_key": ""})
        config = res["json"]["config"]
        self.assertEqual((config["provider"], config["has_key"], config["key_hint"]), ("anthropic", True, "…1111"))
        self.assertEqual(config["saved"]["openai"], {"model": "gpt-x", "base_url": "", "has_key": True, "key_hint": "…2222"})
        self.assertNotIn(b"sk-", f.call("GET", "/api/ai")["body"])  # keys never go back to the browser

        # The model list uses the key saved for the provider asked about, or one just typed.
        seen = []
        real = server.ai.list_models
        server.ai.list_models = lambda c: seen.append((c["provider"], c["api_key"])) or []
        try:
            f.call("POST", "/api/ai/models", {"provider": "openai"})
            f.call("POST", "/api/ai/models", {"provider": "groq", "api_key": "gsk-typed"})
            self.admin.call("POST", "/api/ai/models", {"provider": "openai"})
        finally:
            server.ai.list_models = real
        self.assertEqual(seen, [("openai", "sk-openai-friend-2222"), ("groq", "gsk-typed"), ("openai", "")])
        self.assertEqual(f.call("POST", "/api/ai/models", {"provider": "nope"})["status"], 400)

        # Settings saved before keys were per provider still load.
        db = server.database.UserDB(server.store()[0], self.friend_id)
        db.set_setting("ai_config", json.dumps({"provider": "gemini", "model": "g", "base_url": "", "api_key": "old-key-3333"}))
        config = f.call("GET", "/api/ai")["json"]["config"]
        self.assertEqual((config["provider"], config["model"], config["key_hint"]), ("gemini", "g", "…3333"))
        self.assertEqual(server.ai_config(db)["api_key"], "old-key-3333")

    def test_saved_answers_are_private_and_fill_forms(self):
        a, f = self.admin, self.friend
        a.call("POST", "/api/answers", {"items": [{"question": "Desired salary", "answer": "ADMIN SECRET SALARY"}]})
        admin_answer = a.call("GET", "/api/answers")["json"][0]["id"]
        # Nobody else can read, change or delete it, or have it fill their forms.
        self.assertEqual(f.call("GET", "/api/answers")["json"], [])
        self.assertEqual(f.call("PUT", f"/api/answers/{admin_answer}", {"answer": "hacked"})["status"], 404)
        self.assertEqual(f.call("DELETE", f"/api/answers/{admin_answer}")["status"], 404)
        form = [{"id": "f1", "label": "What is your desired salary?", "kind": "text"},
                {"id": "f2", "label": "Do you have AWS experience?", "kind": "radio", "options": ["Yes", "No"]},
                {"id": "f3", "label": "Gender", "kind": "select", "options": ["Male", "Female", "Decline"]}]
        res = f.call("POST", "/api/autofill", {"fields": form, "job": {"url": "https://jobs.example.com/1"}})["json"]
        self.assertNotIn(b"ADMIN SECRET", json.dumps(res).encode())

        # Their own saved answer fills the form; the AI gets only what's left and isn't personal.
        f.call("POST", "/api/answers", {"items": [{"question": "Salary expectation", "answer": "$150k"}]})
        f.call("PUT", "/api/ai", {"provider": "openai", "model": "m", "api_key": "sk-test-key-1234"})
        asked = []
        real = server.ai.answer_form
        server.ai.answer_form = lambda config, profile, instructions, job, questions, saved: (
            asked.append(([q["label"] for q in questions], job["company"])) or {"f2": "Yes, definitely"})
        try:
            res = f.call("POST", "/api/autofill", {"fields": form, "job": {"url": "https://jobs.example.com/1",
                                                                             "company": "PageCo"}})["json"]
        finally:
            server.ai.answer_form = real
        self.assertEqual(asked, [(["Do you have AWS experience?"], "PageCo")])  # the admin's same-URL job isn't used
        self.assertEqual({x["id"]: (x["value"], x["source"]) for x in res["answers"]},
                         {"f1": ("$150k", "saved"), "f2": ("Yes", "ai")})
        self.assertEqual(res["left"], [{"id": "f3", "label": "Gender", "personal": True}])

        # Remembering skips what's already saved; editing keeps one answer per question.
        filled = [{"label": "Salary expectation", "kind": "text", "value": "$150k"},
                  {"label": "Gender", "kind": "select", "options": ["Male", "Female"], "value": "Female"}]
        cands = f.call("POST", "/api/answers/collect", {"fields": filled, "company": "PageCo"})["json"]["candidates"]
        self.assertEqual([c["question"] for c in cands], ["Gender"])
        mine = f.call("GET", "/api/answers")["json"][0]["id"]
        self.assertEqual(f.call("PUT", f"/api/answers/{mine}", {"answer": "$160k"})["json"]["answer"], "$160k")
        self.assertEqual(f.call("DELETE", f"/api/answers/{mine}")["status"], 200)
        self.assertEqual(f.call("GET", "/api/answers")["json"], [])
        self.assertEqual(a.call("GET", "/api/answers")["json"][0]["answer"], "ADMIN SECRET SALARY")

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

    def test_claude_can_update_the_profile_and_it_can_be_restored(self):
        f = self.friend
        token = f.call("GET", "/api/mcp")["json"]["token"]

        def tool(name, args):
            return Client(bearer=token).call("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                              "params": {"name": name, "arguments": args}})["json"]["result"]

        f.call("PUT", "/api/profile", {"profile": "Friend profile. " * 20, "instructions": "One page."})
        # Dropping most of the profile by accident is refused; asked-for shortening is allowed.
        self.assertTrue(tool("update_profile", {"profile": "Short"})["isError"])
        merged = "Friend profile. " * 20 + "New role from my notes."
        self.assertFalse(tool("update_profile", {"profile": merged}).get("isError"))
        state = f.call("GET", "/api/profile")["json"]
        self.assertEqual((state["profile"], state["instructions"]), (merged, "One page."))
        self.assertEqual(state["previous"]["replaced_by"], "Claude")
        # Restore swaps back; restoring again swaps forward.
        self.assertEqual(f.call("POST", "/api/profile/restore")["json"]["profile"], "Friend profile. " * 20)
        self.assertEqual(f.call("POST", "/api/profile/restore")["json"]["profile"], merged)
        # Their Claude only ever touches their own profile.
        self.assertNotIn(b"New role", self.admin.call("GET", "/api/profile")["body"])
        self.assert_admin_data_intact()

    def test_tailored_resume_downloads_as_a_named_pdf(self):
        res = self.admin.call("GET", f"/api/generated/{self.a_gen}/pdf")
        self.assertEqual(res["status"], 200)
        self.assertEqual(res["headers"]["Content-Type"], "application/pdf")
        self.assertIn("attachment; filename*=UTF-8''Admin%20Resume%20-%20SecretCo.pdf",
                      res["headers"]["Content-Disposition"])
        self.assertTrue(res["body"].startswith(b"%PDF-1.4"))
        self.assertEqual(self.admin.call("GET", f"/api/generated/{self.a_gen}")["json"]["file_name"],
                         "Admin Resume - SecretCo.pdf")

    def test_anyone_can_sign_up(self):
        newcomer = Client()
        newcomer.sign_in("newcomer@example.com", "Newcomer")
        self.assertTrue(newcomer.call("GET", "/api/meta")["json"]["auth"]["ok"])
        self.assertEqual(newcomer.call("GET", "/api/jobs")["json"], [])
        self.assertFalse(newcomer.call("GET", "/api/meta")["json"]["user"]["is_admin"])

    def test_job_limit(self):
        c = Client()
        c.sign_in("busy@example.com")
        uid = self.user_id("busy@example.com")
        self.admin.call("PUT", f"/api/admin/users/{uid}", {"job_limit": 2})
        ids = [c.call("POST", "/api/jobs", {"company": f"Co{i}"})["json"]["id"] for i in range(2)]
        res = c.call("POST", "/api/jobs", {"company": "One too many"})
        self.assertEqual(res["status"], 403)
        self.assertIn("limit", res["json"]["error"])
        self.assertEqual(c.call("POST", "/api/jobs/capture", {"url": "https://x.example.com/9"})["status"], 403)
        # Existing jobs still work, and the admin sees who's at the limit.
        self.assertEqual(c.call("PUT", f"/api/jobs/{ids[0]}", {"status": "applied"})["status"], 200)
        users = self.admin.call("GET", "/api/admin/users")["json"]
        self.assertEqual(users["default_limit"], server.DEFAULT_JOB_LIMIT)
        busy = next(u for u in users["users"] if u["id"] == uid)
        self.assertEqual((busy["jobs"], busy["limit"]), (2, 2))
        self.assertGreaterEqual(self.admin.call("GET", "/api/meta")["json"]["user"]["at_limit"], 1)
        # Deleting a job frees a slot; raising the limit allows more; null goes back to the default.
        c.call("DELETE", f"/api/jobs/{ids[1]}")
        self.assertEqual(c.call("POST", "/api/jobs", {"company": "Again"})["status"], 201)
        self.admin.call("PUT", f"/api/admin/users/{uid}", {"job_limit": 5})
        self.assertEqual(c.call("POST", "/api/jobs", {"company": "More"})["status"], 201)
        self.admin.call("PUT", f"/api/admin/users/{uid}", {"job_limit": None})
        busy = next(u for u in self.admin.call("GET", "/api/admin/users")["json"]["users"] if u["id"] == uid)
        self.assertEqual(busy["limit"], server.DEFAULT_JOB_LIMIT)
        self.assertEqual(self.admin.call("PUT", f"/api/admin/users/{uid}", {"job_limit": -1})["status"], 400)
        self.assertEqual(self.admin.call("PUT", f"/api/admin/users/{uid}", {"job_limit": "lots"})["status"], 400)
        # The admin has no limit.
        self.assertIsNone(next(u for u in self.admin.call("GET", "/api/admin/users")["json"]["users"]
                               if u["id"] == 1)["limit"])

    def test_removing_access_logs_out_everywhere(self):
        c = Client()
        c.sign_in("temp@example.com")
        uid = self.user_id("temp@example.com")
        code = c.call("POST", "/api/extension-code")["json"]["code"]
        extension = Client(bearer=Client().call("POST", "/api/login", {"password": code})["json"]["token"])
        c.call("POST", "/api/jobs", {"company": "TempCo"})
        self.assertEqual(extension.call("GET", "/api/jobs")["status"], 200)

        self.admin.call("PUT", f"/api/admin/users/{uid}", {"status": "blocked"})
        self.assertEqual(c.call("GET", "/api/jobs")["status"], 401)
        self.assertEqual(extension.call("GET", "/api/jobs")["status"], 401)
        again = c.sign_in("temp@example.com")  # signing in again doesn't get them back in
        self.assertIn("turned%20off", again["headers"]["Location"])
        self.assertEqual(c.call("GET", "/api/jobs")["status"], 401)

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

    def test_sponsorship_tags(self):
        f = self.friend
        job = f.call("POST", "/api/jobs", {"company": "NoVisa", "description":
                                           "Great role.\nWe are unable to sponsor visas at this time."})["json"]
        self.assertEqual(job["sponsorship"], "no")
        detail = f.call("GET", f"/api/jobs/{job['id']}")["json"]
        self.assertIn("unable to sponsor", detail["sponsorship_quote"])
        # Editing the description re-reads it; other edits leave it alone.
        f.call("PUT", f"/api/jobs/{job['id']}", {"description": "Visa sponsorship is available."})
        self.assertEqual(f.call("PUT", f"/api/jobs/{job['id']}", {"notes": "x"})["json"]["sponsorship"], "yes")
        # Jobs saved before the check existed are read once when the app starts.
        db, _ = server.store()
        db.execute("UPDATE jobs SET sponsorship = NULL WHERE id = ?", (job["id"],))
        server.check_unchecked_jobs(db)
        self.assertEqual(db.query("SELECT sponsorship FROM jobs WHERE id = ?", (job["id"],))[0]["sponsorship"], "yes")
        # The extension's pre-save check, and turning the tags off.
        r = f.call("POST", "/api/sponsorship", {"description": "Must be a U.S. citizen."})["json"]
        self.assertEqual((r["status"], r["flag"]), ("no", True))
        f.call("PUT", "/api/settings/sponsorship", {"on": False})
        self.assertFalse(f.call("GET", "/api/meta")["json"]["flag_sponsorship"])
        self.assertTrue(self.admin.call("GET", "/api/meta")["json"]["flag_sponsorship"])  # per person
        f.call("PUT", "/api/settings/sponsorship", {"on": True})
        self.assertEqual(Client().call("POST", "/api/sponsorship", {"description": "x"})["status"], 401)

    def test_backup_ai_provider_settings(self):
        f = self.friend
        f.call("PUT", "/api/ai", {"provider": "gemini", "api_key": "gemini-key-123456", "model": "gemini-flash"})
        cfg = f.call("PUT", "/api/ai", {"provider": "groq", "api_key": "groq-key-123456", "model": "big",
                                        "as_backup": True})["json"]["config"]
        self.assertEqual((cfg["provider"], cfg["backup"]), ("gemini", "groq"))  # main provider unchanged
        db, _ = server.store()
        config = server.ai_config(database.UserDB(db, self.friend_id))
        self.assertEqual((config["provider"], config["fallback"]["provider"]), ("gemini", "groq"))
        self.assertNotIn("fallback", server.ai_config(database.UserDB(db, self.friend_id), with_backup=False))
        self.assertNotIn("api_key", json.dumps(f.call("GET", "/api/ai")["json"]))  # keys never go back
        # Clearing it, and refusing a provider with no saved key.
        self.assertEqual(f.call("PUT", "/api/ai/backup", {"provider": ""})["json"]["config"]["backup"], "")
        self.assertEqual(f.call("PUT", "/api/ai/backup", {"provider": "openai"})["status"], 400)
        self.assertEqual(f.call("PUT", "/api/ai/backup", {"provider": "groq"})["json"]["config"]["backup"], "groq")
        # Making the backup the main provider means there's no separate backup any more.
        cfg = f.call("PUT", "/api/ai", {"provider": "groq", "model": "big"})["json"]["config"]
        self.assertEqual((cfg["provider"], cfg["backup"]), ("groq", ""))
        self.assertEqual(self.admin.call("GET", "/api/ai")["json"]["config"]["backup"], "")  # per person

    def test_keys_are_stored_encrypted(self):
        f = self.friend
        f.call("PUT", "/api/ai", {"provider": "openai", "api_key": "sk-PLAINTEXT-friend-998877", "model": "m"})
        db, _ = server.store()
        raw = db.query("SELECT value FROM user_settings WHERE user_id = ? AND key = 'ai_config'", (self.friend_id,))
        self.assertTrue(raw[0]["value"].startswith("enc1:"))
        self.assertNotIn("PLAINTEXT", raw[0]["value"])
        self.assertNotIn("PLAINTEXT", json.dumps(db.query("SELECT * FROM user_settings")))
        user = database.UserDB(db, self.friend_id)
        self.assertEqual(server.ai_config(user, "openai")["api_key"], "sk-PLAINTEXT-friend-998877")  # still usable
        self.assertEqual(f.call("GET", "/api/ai")["json"]["config"]["saved"]["openai"]["key_hint"], "…8877")
        # Google Drive tokens too.
        user.set_setting("google_token", '{"refresh_token": "1//PLAINTEXT-refresh"}')
        self.assertNotIn("PLAINTEXT", json.dumps(db.query("SELECT * FROM user_settings")))
        self.assertIn("PLAINTEXT-refresh", user.get_setting("google_token"))

    def test_mcp_tokens_are_not_stored_readably(self):
        def tools_list(token):
            return Client(bearer=token).call("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["status"]
        f = self.friend
        token = f.call("GET", "/api/mcp")["json"]["token"]
        self.assertEqual(f.call("GET", "/api/mcp")["json"]["token"], token)  # you can still see your own
        db, _ = server.store()
        rows = {r["key"]: r["value"] for r in
                db.query("SELECT key, value FROM user_settings WHERE user_id = ?", (self.friend_id,))}
        self.assertTrue(rows["mcp_token"].startswith("enc1:"))
        self.assertNotIn(token, json.dumps(db.query("SELECT * FROM user_settings")))
        self.assertEqual(rows["mcp_token_hash"], database.token_fingerprint(token))
        self.assertEqual(tools_list(token), 200)
        self.assertEqual(tools_list(rows["mcp_token_hash"]), 401)  # the stored fingerprint isn't a token
        new = f.call("POST", "/api/mcp/rotate")["json"]["token"]
        self.assertEqual((tools_list(token), tools_list(new)), (401, 200))

    def test_plain_text_mcp_tokens_from_before_keep_working(self):
        db, _ = server.store()
        uid = self.user_id("friend@example.com")
        db.run_many([("DELETE FROM user_settings WHERE user_id = ? AND key IN ('mcp_token', 'mcp_token_hash')", (uid,)),
                     ("INSERT INTO user_settings (user_id, key, value) VALUES (?, 'mcp_token', 'old-plain-token-abc')",
                      (uid,))])
        database.encrypt_secrets(db, database.UserDB.keyring)
        self.assertNotIn("old-plain-token-abc", json.dumps(db.query("SELECT * FROM user_settings")))
        res = Client(bearer="old-plain-token-abc").call("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual(res["status"], 200)
        self.assertEqual(self.friend.call("GET", "/api/mcp")["json"]["token"], "old-plain-token-abc")

    def test_plain_text_keys_from_before_get_encrypted(self):
        db, _ = server.store()
        db.execute("INSERT OR REPLACE INTO user_settings (user_id, key, value) VALUES (?, 'ai_config', ?)",
                   (self.friend_id, json.dumps({"provider": "groq", "providers": {"groq": {
                       "api_key": "gsk_OLD-PLAINTEXT", "model": "m", "base_url": ""}}})))
        self.assertEqual(server.ai_config(database.UserDB(db, self.friend_id))["api_key"], "gsk_OLD-PLAINTEXT")
        self.assertGreaterEqual(database.encrypt_secrets(db, database.UserDB.keyring), 1)
        self.assertNotIn("OLD-PLAINTEXT", json.dumps(db.query("SELECT * FROM user_settings")))
        self.assertEqual(server.ai_config(database.UserDB(db, self.friend_id))["api_key"], "gsk_OLD-PLAINTEXT")
        self.assertEqual(database.encrypt_secrets(db, database.UserDB.keyring), 0)  # nothing left to do

    def test_forged_or_anonymous_requests(self):
        self.assertEqual(Client().call("GET", "/api/jobs")["status"], 401)
        forged = Client()
        forged.cookies[server.SESSION_COOKIE] = "session:9999999999:1.0000000000000000000000000000000000000000"
        self.assertEqual(forged.call("GET", "/api/jobs")["status"], 401)


if __name__ == "__main__":
    unittest.main()
