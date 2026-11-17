"""One-time Google sign-in that prints a refresh token for Google Tasks.

Run it on a computer with a web browser (your laptop, not the Pi):

    python3 get_tasks_token.py YOUR_CLIENT_ID YOUR_CLIENT_SECRET [PORT]

A browser tab opens. Sign in with the Google account whose Tasks you want on the
screen and allow read-only access to Tasks. The script then prints three lines
to paste into larapaper/.env on the Pi. Only Python's standard library is used.

Over SSH (e.g. on the Pi), it prints a link to open in your laptop's browser.
After you allow access, the browser lands on an error page; paste that page's
address back into the terminal. No port forwarding is needed.

PORT (default 8799) is only the address Google sends the browser back to.
"""
import http.server
import json
import os
import secrets
import sys
import urllib.parse
import urllib.request
import webbrowser

SCOPE = "https://www.googleapis.com/auth/tasks.readonly"
DEFAULT_PORT = 8799


def main() -> None:
    if len(sys.argv) not in (3, 4):
        sys.exit(__doc__)
    client_id, client_secret = sys.argv[1], sys.argv[2]
    port = int(sys.argv[3]) if len(sys.argv) == 4 else DEFAULT_PORT
    redirect = f"http://127.0.0.1:{port}/"
    state = secrets.token_urlsafe(16)
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",   # always return a refresh token
        "state": state,
    })

    result: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "code" not in q and "error" not in q:
                self.send_response(404); self.end_headers(); return
            result.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Done. You can close this tab and go back to the terminal.")

        def log_message(self, *args):
            pass

    def paste_mode() -> None:
        print("1. Open this link in a browser (your laptop is fine) and sign in:\n\n" + url + "\n")
        print("2. After you click Allow, the browser shows an error page such as")
        print("   \"This site can't be reached\". That's expected.")
        print("3. Copy the whole address from the browser's address bar (it starts with")
        print(f"   http://127.0.0.1:{port}/?state=... and contains code=...) and paste it here.\n")
        while "code" not in result and "error" not in result:
            pasted = input("Address: ").strip()
            q = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query)
            if "code" in q or "error" in q:
                result.update({k: v[0] for k, v in q.items()})
            else:
                print("That address has no code= in it. Copy the full address and try again.")

    server = None
    if not os.environ.get("SSH_CONNECTION"):
        try:
            server = http.server.HTTPServer(("127.0.0.1", port), Handler)
        except OSError:
            server = None
    if server is None:
        # Over SSH, or the port is taken: no local listener, paste the address instead.
        paste_mode()
    else:
        print("Opening your browser to sign in. If it doesn't open, visit:\n\n" + url + "\n")
        webbrowser.open(url)
        while "code" not in result and "error" not in result:
            server.handle_request()
        server.server_close()

    if "error" in result:
        sys.exit(f"Google returned an error: {result['error']}")
    if result.get("state") != state:
        sys.exit("The sign-in response didn't match this request. Run the script again.")

    body = urllib.parse.urlencode({
        "code": result["code"],
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect,
        "grant_type": "authorization_code",
    }).encode()
    with urllib.request.urlopen("https://oauth2.googleapis.com/token", data=body, timeout=15) as resp:
        tokens = json.load(resp)
    refresh = tokens.get("refresh_token")
    if not refresh:
        sys.exit("Google didn't return a refresh token. Remove the app's access at "
                 "https://myaccount.google.com/permissions and run this again.")

    print("Paste these into larapaper/.env on the Pi:\n")
    print(f"GOOGLE_CLIENT_ID={client_id}")
    print(f"GOOGLE_CLIENT_SECRET={client_secret}")
    print(f"GOOGLE_TASKS_REFRESH_TOKEN={refresh}")


if __name__ == "__main__":
    main()
