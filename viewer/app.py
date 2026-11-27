"""Screen viewer: the E1001's screens on your phone, any time.

Shows the latest picture LaraPaper made for every screen in the display's
playlists, in playlist order, and marks the one on the display right now.
It reads LaraPaper's database and picture folder (both mounted read-only).

The Refresh button can also ask LaraPaper to re-fetch and redraw every screen
now, through LaraPaper's own API (LARAPAPER_API_TOKEN). That replaces each
screen's picture but never changes what's on the display or moves the
rotation on. Without the token, Refresh only re-reads the latest pictures.

    GET  /                 the viewer page (add it to your home screen)
    GET  /screens.json     the screens, newest picture of each
    POST /refresh          ask LaraPaper to redraw the screens now
    GET  /img/<id>.png     a screen picture
    GET  /health           ok / not ok (no token needed)

If VIEWER_TOKEN is set, open the page once as /?token=<VIEWER_TOKEN>; the
viewer then remembers your browser with a cookie for a year.

Standard library only, so the image stays small and needs no upgrades.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import sqlite3
import struct
import threading
import time
import urllib.error
import urllib.request
import zlib
from datetime import datetime, timezone
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s viewer: %(message)s")
log = logging.getLogger("viewer")

DB_PATH = Path(os.getenv("LARAPAPER_DB", "/larapaper/database/database.sqlite"))
IMAGES = Path(os.getenv("LARAPAPER_IMAGES", "/larapaper/images"))
TOKEN = os.getenv("VIEWER_TOKEN", "").strip()
TITLE = os.getenv("VIEWER_TITLE", "E1001").strip() or "E1001"
PORT = int(os.getenv("PORT", "8080"))
LARAPAPER_URL = os.getenv("LARAPAPER_URL", "http://app:8080").rstrip("/")
API_TOKEN = os.getenv("LARAPAPER_API_TOKEN", "").strip()
REFRESH_GAP = 20          # seconds: ignore a second Refresh tap sooner than this
COOKIE = "e1001_viewer"
HERE = Path(__file__).parent
IMAGE_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")


# ---------------------------------------------------------------- LaraPaper --

def _connect() -> sqlite3.Connection:
    # mode=ro: never write to LaraPaper's database, even by accident.
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def _picture(image_id: str | None) -> Path | None:
    if not image_id or not IMAGE_ID.match(image_id):
        return None
    for ext in ("png", "bmp"):
        path = IMAGES / f"{image_id}.{ext}"
        if path.is_file():
            return path
    return None


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def _db_time(value: str | None) -> float | None:
    """LaraPaper stores times as 'YYYY-MM-DD HH:MM:SS' in UTC."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:19]).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def load_screens() -> dict:
    """Every active screen in the active playlists, in playlist order."""
    with _connect() as con:
        devices = con.execute("SELECT id, name, current_screen_image FROM devices ORDER BY id").fetchall()
        rows = con.execute(
            """
            SELECT p.id AS plugin_id, p.name AS name, p.current_image AS image,
                   p.plugin_type AS plugin_type, p.data_payload_updated_at AS rendered_at,
                   pi.id AS item_id, pl.device_id AS device_id, pl.name AS playlist
            FROM playlist_items pi
            JOIN playlists pl ON pl.id = pi.playlist_id
            JOIN plugins p ON p.id = pi.plugin_id
            WHERE pl.is_active = 1 AND pi.is_active = 1 AND pi.mashup IS NULL
            ORDER BY pl.device_id, pl.id, pi."order", pi.id
            """
        ).fetchall()

    on_display = {d["current_screen_image"] for d in devices if d["current_screen_image"]}
    screens, seen = [], set()
    for row in rows:
        if row["plugin_id"] in seen:  # the same screen in two playlists shows once
            continue
        seen.add(row["plugin_id"])
        path = _picture(row["image"])
        # A redraw that comes out identical keeps the old file, so "updated" is
        # whichever is newer: the file, or LaraPaper's last redraw of the screen.
        rendered = _db_time(row["rendered_at"])
        stamps = [t for t in (path.stat().st_mtime if path else None, rendered) if t]
        screens.append({
            "id": row["plugin_id"],
            "item_id": row["item_id"],
            "name": row["name"] or "Screen",
            "playlist": row["playlist"],
            "image": f"img/{path.name}?v={int(path.stat().st_mtime)}" if path else None,
            "updated": _iso(max(stamps)) if stamps else None,
            "rendered": _iso(rendered) if rendered else None,
            "can_refresh": bool(API_TOKEN) and row["plugin_type"] == "recipe",
            "on_display": bool(row["image"]) and row["image"] in on_display,
        })
    return {"title": TITLE, "now": _iso(time.time()), "can_refresh": bool(API_TOKEN), "screens": screens}


# ----------------------------------------------------------------- refresh --

_last_refresh = 0.0
_refresh_lock = threading.Lock()


def _ask_larapaper(item_id: int) -> str | None:
    """Queue a re-fetch and redraw of one playlist item. None = queued, else the error."""
    req = urllib.request.Request(
        f"{LARAPAPER_URL}/api/playlist-items/{item_id}/cache/refresh", data=b"", method="POST",
        headers={"Authorization": f"Bearer {API_TOKEN}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return None if resp.status in (200, 202) else f"HTTP {resp.status}"
    except urllib.error.HTTPError as err:
        if err.code == 401:
            return "LaraPaper didn't accept LARAPAPER_API_TOKEN"
        return f"HTTP {err.code}"
    except Exception as err:
        return str(err)


def refresh_screens(only: set[int] | None = None) -> tuple[int, dict]:
    """Ask LaraPaper to redraw the screens (or just `only`, by screen id)."""
    global _last_refresh
    if not API_TOKEN:
        return 501, {"error": "Set LARAPAPER_API_TOKEN in larapaper/.env to let Refresh redraw the screens."}
    with _refresh_lock:
        now = time.time()
        if now - _last_refresh < REFRESH_GAP:
            return 429, {"error": "Already refreshing.", "retry_after": round(REFRESH_GAP - (now - _last_refresh))}
        _last_refresh = now
    screens = [s for s in load_screens()["screens"] if s["can_refresh"] and (not only or s["id"] in only)]
    queued, errors = [], {}
    for s in screens:
        err = _ask_larapaper(s["item_id"])
        if err:
            errors[s["id"]] = err
            log.warning("Refresh of %s failed: %s", s["name"], err)
        else:
            queued.append(s["id"])
    log.info("Asked LaraPaper to redraw %d screen(s)", len(queued))
    status = 202 if queued else 502
    return status, {"requested_at": _iso(now), "queued": queued, "errors": errors}


# -------------------------------------------------------------------- icon --

def _png(size: int) -> bytes:
    """A small e-paper icon (dark frame, light screen, a few lines), drawn by hand."""
    dark, light, ink = (24, 24, 24), (238, 236, 228), (60, 60, 60)
    m, b = size * 0.12, size * 0.06  # margin, bezel
    sx0, sy0, sx1, sy1 = m + b, size * 0.24 + b, size - m - b, size * 0.76 - b
    lines = [(0.34, 0.70), (0.46, 0.55), (0.58, 0.62)]
    raw = bytearray()
    for y in range(size):
        raw.append(0)
        for x in range(size):
            c = dark
            if sx0 <= x < sx1 and sy0 <= y < sy1:
                c = light
                for ly, lw in lines:
                    yy = size * ly + size * 0.04
                    if yy <= y < yy + size * 0.045 and x < sx0 + (sx1 - sx0) * lw and x >= sx0 + size * 0.06:
                        c = ink
            raw += bytes(c)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9)) + chunk(b"IEND", b""))


ICONS = {192: _png(192), 512: _png(512)}
PAGE = (HERE / "index.html").read_text().replace("{{TITLE}}", TITLE)
MANIFEST = json.dumps({
    "name": f"{TITLE} screens",
    "short_name": TITLE,
    "start_url": "./",
    "display": "standalone",
    "background_color": "#181818",
    "theme_color": "#181818",
    "icons": [{"src": f"icon-{s}.png", "sizes": f"{s}x{s}", "type": "image/png", "purpose": "any maskable"} for s in ICONS],
})


# ------------------------------------------------------------------ server --

class Handler(BaseHTTPRequestHandler):
    server_version = "e1001-viewer"

    def log_message(self, fmt, *args):  # quieter than the default, and no tokens in logs
        log.debug("%s %s", self.command, self.path.split("?")[0])

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _authorised(self, query: dict) -> tuple[bool, bool]:
        """(allowed, set_cookie)"""
        if not TOKEN:
            return True, False
        given = query.get("token", [""])[0]
        if given and secrets.compare_digest(given, TOKEN):
            return True, True
        jar = cookies.SimpleCookie(self.headers.get("Cookie", ""))
        got = jar[COOKIE].value if COOKIE in jar else ""
        return bool(got) and secrets.compare_digest(got, TOKEN), False

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        url = urlparse(self.path)
        allowed, _ = self._authorised({})   # cookie only: never a token in a POST address
        if not allowed:
            return self._send(401, b'{"error": "locked"}', "application/json")
        if url.path != "/refresh":
            return self._send(404, b"not found", "text/plain")
        only = None
        length = int(self.headers.get("Content-Length") or 0)
        if 0 < length <= 4096:
            try:
                ids = json.loads(self.rfile.read(length) or b"{}").get("ids")
                only = {int(i) for i in ids} if ids else None
            except (ValueError, TypeError, AttributeError):
                return self._send(400, b'{"error": "bad request"}', "application/json")
        try:
            status, body = refresh_screens(only)
        except Exception as err:
            log.warning("Refresh failed: %s", err)
            status, body = 503, {"error": f"Could not refresh: {err}"}
        return self._send(status, json.dumps(body).encode(), "application/json", {"Cache-Control": "no-store"})

    def do_GET(self):
        url = urlparse(self.path)
        path, query = url.path, parse_qs(url.query)

        # Open to anyone on the network: nothing private in these.
        if path == "/health":
            try:
                with _connect() as con:
                    con.execute("SELECT 1 FROM plugins LIMIT 1")
                return self._send(200, b'{"ok": true}', "application/json")
            except Exception as err:
                return self._send(503, json.dumps({"ok": False, "error": str(err)}).encode(), "application/json")
        if path == "/manifest.webmanifest":
            return self._send(200, MANIFEST.encode(), "application/manifest+json")
        if path in ("/icon-192.png", "/icon-512.png"):
            return self._send(200, ICONS[int(path[6:9])], "image/png", {"Cache-Control": "max-age=604800"})

        allowed, set_cookie = self._authorised(query)
        if not allowed:
            msg = b"<!doctype html><meta name=viewport content='width=device-width'><body style='font:16px system-ui;padding:2em'>" \
                  b"<h1>Locked</h1><p>Open this page once as <code>/?token=...</code> with the VIEWER_TOKEN from larapaper/.env.</p>"
            return self._send(401, msg, "text/html; charset=utf-8")

        if set_cookie and path == "/":
            # Drop the token from the address bar so it isn't saved in a home-screen shortcut.
            c = f"{COOKIE}={TOKEN}; Max-Age=31536000; Path=/; HttpOnly; SameSite=Strict"
            return self._send(303, b"", "text/plain", {"Location": "./", "Set-Cookie": c})

        if path == "/":
            return self._send(200, PAGE.encode(), "text/html; charset=utf-8", {"Cache-Control": "no-cache"})
        if path == "/screens.json":
            try:
                body = json.dumps(load_screens()).encode()
            except Exception as err:
                log.warning("Could not read LaraPaper: %s", err)
                return self._send(503, json.dumps({"error": f"Could not read LaraPaper: {err}"}).encode(), "application/json")
            return self._send(200, body, "application/json", {"Cache-Control": "no-store"})
        m = re.fullmatch(r"/img/([A-Za-z0-9-]{8,64})\.(png|bmp)", path)
        if m:
            file = IMAGES / f"{m.group(1)}.{m.group(2)}"
            if file.is_file():
                ctype = "image/png" if m.group(2) == "png" else "image/bmp"
                # Each picture's name never changes content, so the phone can keep it.
                return self._send(200, file.read_bytes(), ctype, {"Cache-Control": "private, max-age=86400"})
        return self._send(404, b"not found", "text/plain")


if __name__ == "__main__":
    if not TOKEN:
        log.warning("VIEWER_TOKEN is empty: anyone on your home network can open the viewer.")
    if not API_TOKEN:
        log.info("LARAPAPER_API_TOKEN is empty: Refresh only re-reads the latest pictures.")
    log.info("Reading %s and %s; listening on port %d", DB_PATH, IMAGES, PORT)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
