"""Reminders for This Week, read from Google Tasks.

Google Tasks shows up in Google Calendar's sidebar as "Tasks", but it isn't a
real calendar and has no iCal link, so it's read through the Google Tasks API
with a refresh token (see tools/get_tasks_token.py).
"""
import json
import logging
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta

import config

log = logging.getLogger("tasks")

API = "https://tasks.googleapis.com/tasks/v1"

_token: tuple[float, str] | None = None        # (expires_at, access_token)
_cache: tuple[float, list[dict]] | None = None  # (fetched_at, raw tasks)


def configured() -> bool:
    return bool(config.GOOGLE_CLIENT_ID and config.GOOGLE_CLIENT_SECRET
                and config.GOOGLE_TASKS_REFRESH_TOKEN)


def _access_token() -> str:
    global _token
    if _token and time.time() < _token[0] - 60:
        return _token[1]
    body = urllib.parse.urlencode({
        "client_id": config.GOOGLE_CLIENT_ID,
        "client_secret": config.GOOGLE_CLIENT_SECRET,
        "refresh_token": config.GOOGLE_TASKS_REFRESH_TOKEN,
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=body)
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.load(resp)
    _token = (time.time() + int(data.get("expires_in", 3600)), data["access_token"])
    return _token[1]


def _get(path: str, params: dict | None = None) -> dict:
    url = f"{API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {_access_token()}"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)


def _fetch_all() -> list[dict]:
    """Every open, top-level task in the chosen lists."""
    wanted = {name.lower() for name in config.TASKS_LISTS}
    lists = _get("/users/@me/lists", {"maxResults": 100}).get("items", [])
    out = []
    for tl in lists:
        if wanted and tl.get("title", "").lower() not in wanted:
            continue
        page = None
        while True:
            params = {"showCompleted": "false", "showHidden": "false", "maxResults": 100}
            if page:
                params["pageToken"] = page
            data = _get(f"/lists/{urllib.parse.quote(tl['id'])}/tasks", params)
            out += [t for t in data.get("items", []) if not t.get("parent")]   # skip subtasks
            page = data.get("nextPageToken")
            if not page:
                break
    return out


def _raw() -> list[dict] | None:
    global _cache
    if _cache and time.time() - _cache[0] < config.CACHE_SECONDS:
        return _cache[1]
    try:
        tasks = _fetch_all()
        _cache = (time.time(), tasks)
        return tasks
    except Exception as exc:
        log.warning("Google Tasks fetch failed: %s", exc)
        return _cache[1] if _cache else None   # keep showing the last good list


def _when(due: date | None, today: date) -> str:
    if due is None:
        return ""
    days = (due - today).days
    if days < 0:
        return "OVERDUE"
    if days == 0:
        return "TODAY"
    if days == 1:
        return "TMRW"
    return due.strftime("%a").upper()


def _sample(today: date) -> list[dict]:
    return [
        {"title": "Library books", "due": today - timedelta(days=1)},
        {"title": "Swap furnace filter", "due": today},
        {"title": "RSVP: Thanksgiving dinner", "due": today + timedelta(days=2)},
        {"title": "Order holiday cards", "due": today + timedelta(days=5)},
    ]


def reminders(today: date) -> list[dict] | None:
    """[{title, when, overdue}] due by today + TASKS_DAYS_AHEAD, soonest first.

    None means Google Tasks isn't set up (or has never loaded), so the caller
    falls back to the list in content.yml.
    """
    if not configured():
        if config.SAMPLE_DATA != "on":
            return None
        items = _sample(today)
    else:
        raw = _raw()
        if raw is None:
            return None
        items = []
        for t in raw:
            title = (t.get("title") or "").strip()
            if not title:
                continue
            due = date.fromisoformat(t["due"][:10]) if t.get("due") else None
            items.append({"title": title, "due": due})

    horizon = today + timedelta(days=config.TASKS_DAYS_AHEAD)
    keep = [i for i in items
            if (i["due"] is None and config.TASKS_INCLUDE_UNDATED)
            or (i["due"] is not None and i["due"] <= horizon)]
    keep.sort(key=lambda i: (i["due"] is None, i["due"] or date.max))
    return [{"title": i["title"], "when": _when(i["due"], today),
             "overdue": i["due"] is not None and i["due"] < today} for i in keep]
