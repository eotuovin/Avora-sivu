"""End-to-end tests for server.py with a fake Luuri and a fake ux-events tracker.

    python3 -m unittest discover -s tests
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Fake:
    """Records POSTs; answers like Luuri's /api/companies and /contacts."""

    def __init__(self):
        self.calls = []
        self.up = True
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                if not fake.up:
                    self.send_response(503); self.end_headers(); return
                fake.calls.append((self.path, self.headers.get("Authorization"), body))
                if self.path == "/api/companies":
                    out = {"id": "c0ffee00-0000-4000-8000-000000000001"}
                elif self.path.endswith("/contacts"):
                    out = {"id": "contact-1"}
                else:
                    self.send_response(204); self.end_headers(); return
                data = json.dumps(out).encode()
                self.send_response(201); self.send_header("Content-Length", str(len(data))); self.end_headers()
                self.wfile.write(data)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"


FAKE = Fake()
TMP = tempfile.mkdtemp()
os.environ.update({
    "AVORA_DB_PATH": os.path.join(TMP, "avora.db"),
    "LUURI_URL": FAKE.url,
    "LUURI_API_KEY": "test-key",
    "ANALYTICS_UX_EVENTS_URL": FAKE.url + "/api/ux-events",
    "AVORA_RETRY_SECONDS": "0.2",
    "AVORA_SITE_ENV": "testing",
})
sys.path.insert(0, str(ROOT))
import server  # noqa: E402

server.init_db()
APP = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
threading.Thread(target=APP.serve_forever, daemon=True).start()
threading.Thread(target=server.delivery_loop, daemon=True).start()
BASE = f"http://127.0.0.1:{APP.server_address[1]}"


def post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


GOOD = {"name": "Testi Henkilö", "phone": "0401234567", "email": "", "company": "Kampaamo Testi, Kuopio",
        "message": "Varaukset nyt vihossa", "call_time": "ip", "package": "kasvu", "consent": True, "industry_example": "k"}


class ServerTests(unittest.TestCase):
    def setUp(self):
        FAKE.calls.clear()
        FAKE.up = True
        server._rate.clear()

    def test_pages_are_served_and_hidden_from_search(self):
        for path in ("/", "/tietosuoja", "/runtime.js", "/robots.txt"):
            with urllib.request.urlopen(BASE + path, timeout=5) as r:
                self.assertEqual(r.status, 200, path)
                self.assertEqual(r.headers["X-Robots-Tag"], "noindex, nofollow")
        with urllib.request.urlopen(BASE + "/", timeout=5) as r:
            html = r.read().decode()
        self.assertIn("/api/esittelypyynto", html)
        self.assertIn('href="/tietosuoja"', html)
        self.assertNotIn(".dc.html", html)

    def test_demo_request_is_stored_and_delivered_to_luuri(self):
        self.assertEqual(post("/api/esittelypyynto", GOOD), 201)
        self.assertTrue(wait_for(lambda: len(FAKE.calls) >= 2))
        (p1, auth1, company), (p2, auth2, contact) = FAKE.calls[:2]
        self.assertEqual(p1, "/api/companies")
        self.assertEqual(auth1, "Bearer test-key")
        self.assertEqual(company["name"], "Kampaamo Testi, Kuopio")
        self.assertIn("Toivottu soittoaika: iltapäivä", company["notes"])
        self.assertIn("Kiinnostava paketti: Kasvu", company["notes"])
        self.assertEqual(p2, "/api/companies/c0ffee00-0000-4000-8000-000000000001/contacts")
        self.assertEqual(contact["phone"], "0401234567")
        self.assertTrue(contact["is_primary"])

    def test_request_waits_while_luuri_is_down_and_is_delivered_later(self):
        FAKE.up = False
        self.assertEqual(post("/api/esittelypyynto", dict(GOOD, company="")), 201)
        time.sleep(0.6)
        self.assertEqual(FAKE.calls, [])
        with urllib.request.urlopen(BASE + "/api/health", timeout=5) as r:
            self.assertGreaterEqual(json.loads(r.read())["demo_requests"]["waiting"], 1)
        FAKE.up = True
        # Earlier tests may have left requests in the same outbox; ours must arrive too.
        self.assertTrue(wait_for(lambda: any(
            c[0] == "/api/companies" and c[2]["name"] == "Testi Henkilö (Avora-esittelypyyntö)" for c in FAKE.calls)))

    def test_invalid_requests_are_refused(self):
        self.assertEqual(post("/api/esittelypyynto", dict(GOOD, consent=False)), 400)
        self.assertEqual(post("/api/esittelypyynto", dict(GOOD, name="")), 400)
        self.assertEqual(post("/api/esittelypyynto", dict(GOOD, phone="", email="ei-sahkoposti")), 400)
        self.assertEqual(post("/api/esittelypyynto", dict(GOOD, package="ilmainen")), 400)
        self.assertEqual(post("/api/esittelypyynto", dict(GOOD, message="x" * 5000)), 400)

    def test_rate_limit(self):
        codes = [post("/api/esittelypyynto", GOOD) for _ in range(6)]
        self.assertEqual(codes[:5], [201] * 5)
        self.assertEqual(codes[5], 429)

    def test_analytics_batch_is_relayed_with_site_env(self):
        sid = str(uuid.uuid4())
        ev = {"type": "avora_cta_click", "ts": 1, "event_id": str(uuid.uuid4()), "cta": "Varaa esittely", "nested": {"x": 1}}
        self.assertEqual(post("/api/ux", {"session_id": sid, "events": [ev]}), 204)
        self.assertTrue(wait_for(lambda: any(c[0] == "/api/ux-events" for c in FAKE.calls)))
        batch = next(c[2] for c in FAKE.calls if c[0] == "/api/ux-events")
        self.assertEqual(batch["session_id"], sid)
        sent = batch["events"][0]
        self.assertEqual(sent["site_env"], "testing")
        self.assertEqual(sent["cta"], "Varaa esittely")
        self.assertNotIn("nested", sent)

    def test_analytics_refuses_foreign_events(self):
        sid = str(uuid.uuid4())
        self.assertEqual(post("/api/ux", {"session_id": sid, "events": [{"type": "booking_confirmed", "ts": 1}]}), 400)
        self.assertEqual(post("/api/ux", {"session_id": "nope", "events": [{"type": "avora_x", "ts": 1}]}), 400)


if __name__ == "__main__":
    unittest.main()
