"""Avora website: static pages + demo-request intake + analytics relay.

Standard library only (the image is built on the shared production VPS).

  GET  /, /tietosuoja, /runtime.js, /robots.txt   static files from site/
  GET  /api/health                                 liveness + outbox counts
  POST /api/esittelypyynto                         a demo request from the form
  POST /api/ux                                     analytics batch → existing ux-events tracker

Demo requests are written to SQLite first (the outbox), then delivered to
Luuri, the sales desk, as a company + contact. Until Luuri is reachable and
accepts our API key, requests wait in the outbox and are retried; nothing is
lost if Luuri is down.
"""
import json
import logging
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger("avora")

SITE_DIR = Path(os.environ.get("AVORA_SITE_DIR", Path(__file__).with_name("site")))
DB_PATH = os.environ.get("AVORA_DB_PATH", "/data/avora.db")
PORT = int(os.environ.get("AVORA_PORT", "8080"))
LUURI_URL = (os.environ.get("LUURI_URL") or "").rstrip("/")
LUURI_API_KEY = os.environ.get("LUURI_API_KEY") or ""
UX_EVENTS_URL = os.environ.get("ANALYTICS_UX_EVENTS_URL") or ""
SITE_ENV = os.environ.get("AVORA_SITE_ENV", "testing")
RETRY_SECONDS = float(os.environ.get("AVORA_RETRY_SECONDS", "60"))
MAX_ATTEMPTS = 500  # ~8 h at 60 s; the row stays in the DB either way

ROUTES = {"/": "index.html", "/index.html": "index.html", "/tietosuoja": "tietosuoja.html",
          "/runtime.js": "runtime.js", "/robots.txt": "robots.txt"}
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".txt": "text/plain; charset=utf-8"}

CALL_TIMES = {"": "ei valittu", "aamu": "aamupäivä", "ip": "iltapäivä", "ilta": "ilta", "any": "ihan sama"}
PACKAGES = {"": "ei valittu", "start": "Start", "kasvu": "Kasvu", "kokonaisuus": "Kokonaisuus", "eos": "ei osaa sanoa"}
INDUSTRIES = {"": "", "k": "kampaamo", "a": "autokorjaamo", "m": "muuttofirma"}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_db_lock = threading.Lock()
_wake = threading.Event()
_rate: dict[str, list[float]] = {}
_rate_lock = threading.Lock()


# ----------------------------------------------------------------- storage
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with _db_lock, db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS demo_requests (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                payload TEXT NOT NULL,
                luuri_company_id TEXT,
                delivered_at TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT
            )"""
        )


def outbox_counts() -> dict:
    with _db_lock, db() as conn:
        row = conn.execute(
            "SELECT count(*) AS total, sum(delivered_at IS NOT NULL) AS delivered FROM demo_requests"
        ).fetchone()
    total, delivered = row["total"] or 0, row["delivered"] or 0
    return {"total": total, "delivered": delivered, "waiting": total - delivered}


# ------------------------------------------------------------- validation
def clean(value, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("bad type")
    value = value.strip()
    if len(value) > limit:
        raise ValueError("too long")
    return value


def validate_request(body: dict) -> dict:
    out = {
        "name": clean(body.get("name"), 200),
        "phone": clean(body.get("phone"), 50),
        "email": clean(body.get("email"), 200),
        "company": clean(body.get("company"), 200),
        "message": clean(body.get("message"), 2000),
        "call_time": clean(body.get("call_time"), 10),
        "package": clean(body.get("package"), 20),
        "industry_example": clean(body.get("industry_example"), 5),
    }
    if not out["name"]:
        raise ValueError("name required")
    if not out["phone"] and not EMAIL_RE.match(out["email"]):
        raise ValueError("phone or email required")
    if out["email"] and not EMAIL_RE.match(out["email"]):
        raise ValueError("bad email")
    if body.get("consent") is not True:
        raise ValueError("consent required")
    if out["call_time"] not in CALL_TIMES or out["package"] not in PACKAGES or out["industry_example"] not in INDUSTRIES:
        raise ValueError("bad choice")
    return out


def rate_limited(ip: str, limit: int, window: float) -> bool:
    now = time.monotonic()
    with _rate_lock:
        hits = [t for t in _rate.get(ip, []) if now - t < window]
        if len(hits) >= limit:
            _rate[ip] = hits
            return True
        hits.append(now)
        _rate[ip] = hits
        return False


# ------------------------------------------------------- delivery to Luuri
def luuri_notes(p: dict, created_at: str) -> str:
    lines = [
        f"Avora-esittelypyyntö verkkosivulta {created_at[:16].replace('T', ' ')} (UTC).",
        f"Toivottu soittoaika: {CALL_TIMES[p['call_time']]}",
        f"Kiinnostava paketti: {PACKAGES[p['package']]}",
    ]
    if p["company"]:
        lines.append(f"Yritys ja ala (asiakkaan sanoin): {p['company']}")
    if p["message"]:
        lines.append(f"Viesti: {p['message']}")
    if p["industry_example"]:
        lines.append(f"Katsoi sivulla esimerkkiä: {INDUSTRIES[p['industry_example']]}")
    return "\n".join(lines)


def luuri_post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        LUURI_URL + path,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {LUURI_API_KEY}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read() or b"{}")


def deliver_one(row: sqlite3.Row) -> None:
    p = json.loads(row["payload"])
    company_id = row["luuri_company_id"]
    if not company_id:
        name = p["company"] or f"{p['name']} (Avora-esittelypyyntö)"
        company = luuri_post("/api/companies", {"name": name[:200], "notes": luuri_notes(p, row["created_at"]), "source": "manual"})
        company_id = str(company["id"])
        # Remember the company at once so a failed contact step never creates a second company.
        with _db_lock, db() as conn:
            conn.execute("UPDATE demo_requests SET luuri_company_id=? WHERE id=?", (company_id, row["id"]))
    luuri_post(
        f"/api/companies/{company_id}/contacts",
        {"name": p["name"], "phone": p["phone"] or None, "email": p["email"] or None, "is_primary": True, "source": "manual"},
    )


def deliver_pending() -> None:
    if not (LUURI_URL and LUURI_API_KEY):
        return
    with _db_lock, db() as conn:
        rows = conn.execute(
            "SELECT * FROM demo_requests WHERE delivered_at IS NULL AND attempts < ? ORDER BY created_at LIMIT 20",
            (MAX_ATTEMPTS,),
        ).fetchall()
    for row in rows:
        try:
            deliver_one(row)
        except urllib.error.HTTPError as e:
            err = f"HTTP {e.code}"
        except Exception as e:  # network, timeout, bad JSON
            err = type(e).__name__
        else:
            with _db_lock, db() as conn:
                conn.execute(
                    "UPDATE demo_requests SET delivered_at=?, attempts=attempts+1, last_error=NULL WHERE id=?",
                    (datetime.now(timezone.utc).isoformat(), row["id"]),
                )
            log.info("demo request %s delivered to Luuri", row["id"])
            continue
        with _db_lock, db() as conn:
            conn.execute("UPDATE demo_requests SET attempts=attempts+1, last_error=? WHERE id=?", (err, row["id"]))
        log.warning("demo request %s not delivered yet: %s", row["id"], err)
        break  # Luuri is likely down or refusing; try the rest on the next round


def delivery_loop() -> None:
    while True:
        try:
            deliver_pending()
        except Exception:
            log.exception("delivery round failed")
        _wake.wait(RETRY_SECONDS)
        _wake.clear()


# ------------------------------------------------------ analytics relay
def relay_ux(batch: dict) -> None:
    if not UX_EVENTS_URL:
        return
    req = urllib.request.Request(
        UX_EVENTS_URL, data=json.dumps(batch).encode(), headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        urllib.request.urlopen(req, timeout=10).close()
    except Exception as e:
        log.warning("ux-events relay failed: %s", type(e).__name__)


def sanitize_ux(body: dict) -> dict:
    sid = body.get("session_id")
    if uuid.UUID(str(sid)).version != 4:
        raise ValueError("session_id must be a UUIDv4")
    events = body.get("events")
    if not isinstance(events, list) or not 0 < len(events) <= 50:
        raise ValueError("bad events")
    out = []
    for ev in events:
        if not isinstance(ev, dict) or not str(ev.get("type", "")).startswith("avora_"):
            raise ValueError("bad event")
        ev = {k: v for k, v in ev.items() if isinstance(v, (str, int, float, bool)) and len(str(v)) <= 200}
        ev["site_env"] = SITE_ENV
        ev["interaction_surface"] = "avora_web"
        out.append(ev)
    return {"session_id": str(sid), "events": out}


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "avora"
    sys_version = ""

    def log_message(self, fmt, *args):  # no client IPs or query strings in the logs
        pass

    def _client_ip(self) -> str:
        return self.client_address[0]

    def _headers(self, status: int, ctype: str, length: int, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _json(self, status: int, obj: dict) -> None:
        data = json.dumps(obj).encode()
        self._headers(status, "application/json", len(data), {"Cache-Control": "no-store"})
        self.wfile.write(data)

    def _body(self, limit: int) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > limit:
            raise ValueError("bad length")
        data = json.loads(self.rfile.read(n))
        if not isinstance(data, dict):
            raise ValueError("bad body")
        return data

    def do_GET(self):
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        if path == "/api/health":
            return self._json(200, {"ok": True, "demo_requests": outbox_counts(), "luuri_configured": bool(LUURI_URL and LUURI_API_KEY)})
        name = ROUTES.get(path)
        if not name:
            data = b"Sivua ei loydy."
            self._headers(404, "text/plain; charset=utf-8", len(data))
            return self.wfile.write(data)
        f = SITE_DIR / name
        data = f.read_bytes()
        cache = "no-cache" if name.endswith(".html") else "public, max-age=300"
        self._headers(200, TYPES[f.suffix], len(data), {"Cache-Control": cache})
        self.wfile.write(data)

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/esittelypyynto":
            if rate_limited("lead:" + self._client_ip(), 5, 600):
                return self._json(429, {"ok": False})
            try:
                p = validate_request(self._body(16_000))
            except (ValueError, json.JSONDecodeError):
                return self._json(400, {"ok": False})
            rid = str(uuid.uuid4())
            with _db_lock, db() as conn:
                conn.execute(
                    "INSERT INTO demo_requests (id, created_at, payload) VALUES (?, ?, ?)",
                    (rid, datetime.now(timezone.utc).isoformat(), json.dumps(p, ensure_ascii=False)),
                )
            log.info("demo request %s stored", rid)
            _wake.set()
            return self._json(201, {"ok": True})
        if path == "/api/ux":
            if rate_limited("ux:" + self._client_ip(), 60, 60):
                return self._json(429, {"ok": False})
            try:
                batch = sanitize_ux(self._body(64_000))
            except (ValueError, TypeError, json.JSONDecodeError):
                return self._json(400, {"ok": False})
            threading.Thread(target=relay_ux, args=(batch,), daemon=True).start()
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._json(404, {"ok": False})


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    init_db()
    if not (LUURI_URL and LUURI_API_KEY):
        log.warning("LUURI_URL / LUURI_API_KEY not set: demo requests are stored but not delivered")
    if not UX_EVENTS_URL:
        log.warning("ANALYTICS_UX_EVENTS_URL not set: analytics events are dropped")
    threading.Thread(target=delivery_loop, daemon=True).start()
    log.info("listening on :%d", PORT)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
