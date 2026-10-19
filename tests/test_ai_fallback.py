"""Retrying a busy AI provider for a short while, then falling back to the backup provider."""
import json
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ai  # noqa: E402

# What each fake provider answers, in order: an HTTP status (200 means a normal reply). The last one repeats.
SCRIPTS = {}
CALLS = {}


class FakeProvider(BaseHTTPRequestHandler):
    def do_POST(self):
        name = self.path.split("/")[1]
        self.rfile.read(int(self.headers["Content-Length"]))
        CALLS[name] = CALLS.get(name, 0) + 1
        script = SCRIPTS[name]
        status = script[min(CALLS[name], len(script)) - 1]
        body = ({"choices": [{"message": {"content": f"hello from {name}"}, "finish_reason": "stop"}]}
                if status == 200 else {"error": {"message": f"{name} says {status}"}})
        out = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *args):
        pass


class FallbackTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeProvider)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.saved = (ai.RETRY_WAITS, ai.RETRY_WINDOW)
        ai.RETRY_WAITS = (0.01, 0.02)  # keep the tests fast

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        ai.RETRY_WAITS, ai.RETRY_WINDOW = cls.saved

    def setUp(self):
        SCRIPTS.clear()
        CALLS.clear()
        ai.RETRY_WINDOW = 45

    def config(self, name, backup=None):
        c = {"provider": "custom", "api_key": "k", "model": "m", "base_url": f"{self.base}/{name}"}
        if backup:
            c["fallback"] = {"provider": "groq", "api_key": "k", "model": "m", "base_url": f"{self.base}/{backup}"}
        return c

    def ask(self, config):
        return ai.complete(config, "system", [{"role": "user", "content": "hi"}])

    def test_retries_a_busy_provider_then_succeeds(self):
        SCRIPTS["main"] = [503, 503, 200]
        self.assertEqual(self.ask(self.config("main")), "hello from main")
        self.assertEqual(CALLS["main"], 3)
        self.assertEqual(ai.answered_by_backup(), "")

    def test_gives_up_after_three_tries_without_a_backup(self):
        SCRIPTS["main"] = [503]
        with self.assertRaises(ai.AIError) as e:
            self.ask(self.config("main"))
        self.assertIn("overloaded", str(e.exception))
        self.assertEqual(CALLS["main"], 3)

    def test_backup_answers_when_main_stays_busy(self):
        SCRIPTS.update(main=[503], backup=[200])
        self.assertEqual(self.ask(self.config("main", "backup")), "hello from backup")
        self.assertEqual((CALLS["main"], CALLS["backup"]), (3, 1))
        self.assertEqual(ai.answered_by_backup(), "Groq")

    def test_out_of_quota_goes_straight_to_the_backup(self):
        SCRIPTS.update(main=[429], backup=[200])
        self.assertEqual(self.ask(self.config("main", "backup")), "hello from backup")
        self.assertEqual(CALLS["main"], 1)  # retrying a used-up quota won't help

    def test_bad_key_is_not_retried(self):
        SCRIPTS["main"] = [401]
        with self.assertRaises(ai.AIError):
            self.ask(self.config("main"))
        self.assertEqual(CALLS["main"], 1)

    def test_unreachable_provider_goes_to_the_backup_without_waiting(self):
        SCRIPTS["backup"] = [200]
        config = self.config("main", "backup")
        config["base_url"] = "http://127.0.0.1:9/nothing-here"
        self.assertEqual(self.ask(config), "hello from backup")

    def test_stops_retrying_after_the_retry_window(self):
        ai.RETRY_WINDOW = 0.005  # shorter than the first wait
        SCRIPTS["main"] = [503]
        with self.assertRaises(ai.AIError):
            self.ask(self.config("main"))
        self.assertEqual(CALLS["main"], 1)

    def test_both_failing_names_both(self):
        SCRIPTS.update(main=[503], backup=[429])
        started = time.monotonic()
        with self.assertRaises(ai.AIError) as e:
            self.ask(self.config("main", "backup"))
        message = str(e.exception)
        self.assertIn("main says 503", message)
        self.assertIn("Backup (Groq)", message)
        self.assertIn("backup says 429", message)
        self.assertLess(time.monotonic() - started, 5)


if __name__ == "__main__":
    unittest.main()
