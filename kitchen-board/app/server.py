"""Tiny HTTP server for the This Week calendar screen.

  /api/week              JSON {"html": ...} that the LaraPaper recipe polls
  /week                  the full page, to preview in a browser
  /fonts/<file>          fonts for the renderer
  /healthz               liveness check
"""
import json
import logging
import mimetypes
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import board
import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("server")

FONT_DIR = os.path.join(os.path.dirname(__file__), "fonts")
PREVIEW_SHELL = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=800"><title>{title}</title>
<style>html,body{{margin:0;background:#888}}</style></head><body>{body}</body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "kitchen-board/1.0"

    def _send(self, status: int, body: bytes, ctype: str, cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        try:
            if path == "/api/week":
                page = "week"
                payload = {
                    "html": board.render_fragment(page),
                    "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "sample_data": board.calendar_data.using_sample(),
                }
                self._send(200, json.dumps(payload).encode(), "application/json")
            elif path == "/week":
                page = "week"
                html = PREVIEW_SHELL.format(title=page, body=board.render(page, preview=True))
                self._send(200, html.encode(), "text/html; charset=utf-8")
            elif path.startswith("/fonts/"):
                name = os.path.basename(path)
                full = os.path.join(FONT_DIR, name)
                if not os.path.isfile(full):
                    self._send(404, b"not found", "text/plain")
                    return
                with open(full, "rb") as fh:
                    data = fh.read()
                ctype = mimetypes.guess_type(name)[0] or "font/ttf"
                self._send(200, data, ctype, cache="public, max-age=86400")
            elif path == "/healthz":
                self._send(200, b"ok", "text/plain")
            elif path == "/":
                links = '<p><a href="/week">week</a></p>'
                self._send(200, PREVIEW_SHELL.format(title="calendar screens", body=links).encode(),
                           "text/html; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain")
        except Exception:
            log.exception("error serving %s", path)
            self._send(500, b"error", "text/plain")

    def log_message(self, fmt: str, *args) -> None:
        log.info("%s %s", self.address_string(), fmt % args)


def main() -> None:
    httpd = ThreadingHTTPServer(("0.0.0.0", config.PORT), Handler)
    log.info("calendar screens on :%s (tz %s, sample=%s)", config.PORT, config.TZ_NAME,
             board.calendar_data.using_sample())
    httpd.serve_forever()


if __name__ == "__main__":
    main()
