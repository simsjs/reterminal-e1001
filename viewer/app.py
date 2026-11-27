"""Screen viewer: the E1001's screens on your phone, any time.

Shows the latest picture LaraPaper made for every screen in the display's
playlists, in playlist order, and marks the one on the display right now.
It only reads LaraPaper's database and picture folder (both mounted
read-only), so it never re-renders a screen or moves the rotation on.

    GET /                  the viewer page (add it to your home screen)
    GET /screens.json      the screens, newest picture of each
    GET /img/<id>.png      a screen picture
    GET /health            ok / not ok (no token needed)

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
import time
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


def load_screens() -> dict:
    """Every active screen in the active playlists, in playlist order."""
    with _connect() as con:
        devices = con.execute("SELECT id, name, current_screen_image FROM devices ORDER BY id").fetchall()
        rows = con.execute(
            """
            SELECT p.id AS plugin_id, p.name AS name, p.current_image AS image,
                   pl.device_id AS device_id, pl.name AS playlist
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
        screens.append({
            "id": row["plugin_id"],
            "name": row["name"] or "Screen",
            "playlist": row["playlist"],
            "image": f"img/{path.name}?v={int(path.stat().st_mtime)}" if path else None,
            "updated": _iso(path.stat().st_mtime) if path else None,
            "on_display": bool(row["image"]) and row["image"] in on_display,
        })
    return {"title": TITLE, "now": _iso(time.time()), "screens": screens}


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
            c = f"{COOKIE}={TOKEN}; Max-Age=31536000; Path=/; HttpOnly; SameSite=Lax"
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
    log.info("Reading %s and %s; listening on port %d", DB_PATH, IMAGES, PORT)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
