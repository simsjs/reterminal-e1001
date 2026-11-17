"""One-time Google sign-in that prints a refresh token for Google Tasks.

Run it on a computer with a web browser (your laptop, not the Pi):

    python3 get_tasks_token.py YOUR_CLIENT_ID YOUR_CLIENT_SECRET

A browser tab opens. Sign in with the Google account whose Tasks you want on the
screen and allow read-only access to Tasks. The script then prints three lines
to paste into larapaper/.env on the Pi. Only Python's standard library is used.
"""
import http.server
import json
import secrets
import sys
import urllib.parse
import urllib.request
import webbrowser

SCOPE = "https://www.googleapis.com/auth/tasks.readonly"
PORT = 8765


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    client_id, client_secret = sys.argv[1], sys.argv[2]
    redirect = f"http://127.0.0.1:{PORT}/"
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

    server = http.server.HTTPServer(("127.0.0.1", PORT), Handler)
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
