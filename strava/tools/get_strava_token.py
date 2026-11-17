"""One-time Strava sign-in that prints a refresh token for the Fitness screen.

    python3 get_strava_token.py YOUR_CLIENT_ID YOUR_CLIENT_SECRET

Get the client ID and secret from https://www.strava.com/settings/api (set
"Authorization Callback Domain" to localhost). The script prints a link: open it
in a browser, sign in to Strava and click Authorize. The browser then lands on
an error page ("This site can't be reached"); copy that page's whole address and
paste it into the terminal. The script prints three lines to paste into
larapaper/.env on the Pi.

Works the same on your laptop or over SSH on the Pi. Only Python's standard
library is used. The screen only asks for read access.
"""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

SCOPE = "read,activity:read_all"
REDIRECT = "http://localhost/exchange_token"


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    client_id, client_secret = sys.argv[1].strip(), sys.argv[2].strip()
    url = "https://www.strava.com/oauth/authorize?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "approval_prompt": "force",
        "scope": SCOPE,
    })
    print("\n1. Open this link in a browser and click Authorize:\n")
    print("   " + url + "\n")
    print("2. The browser then shows an error page. Copy that page's whole address")
    print("   (it starts with http://localhost/exchange_token) and paste it here.\n")
    pasted = input("Address: ").strip()
    query = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query)
    if "error" in query:
        sys.exit(f"Strava said: {query['error'][0]}. Run the script again and click Authorize.")
    code = (query.get("code") or [""])[0]
    if not code:
        sys.exit("That address has no code in it. Copy the whole address from the browser's address bar.")
    granted = (query.get("scope") or [""])[0]
    if "activity:read_all" not in granted and "activity:read" not in granted:
        sys.exit("Activity access wasn't allowed. Run the script again and leave the activity box ticked.")

    body = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
    }).encode()
    try:
        with urllib.request.urlopen("https://www.strava.com/oauth/token", data=body, timeout=30) as r:
            data = json.load(r)
    except urllib.error.HTTPError as err:
        sys.exit(f"Strava refused the code ({err.code}): {err.read().decode()[:300]}")

    who = data.get("athlete") or {}
    print(f"\nSigned in as {who.get('firstname', '')} {who.get('lastname', '')}. Paste these into larapaper/.env:\n")
    print(f"STRAVA_CLIENT_ID={client_id}")
    print(f"STRAVA_CLIENT_SECRET={client_secret}")
    print(f"STRAVA_REFRESH_TOKEN={data['refresh_token']}\n")


if __name__ == "__main__":
    main()
