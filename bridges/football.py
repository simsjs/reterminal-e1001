"""
Premier League screen for the bridges service.

Shows the next Premier League matchweek: every fixture grouped by day with its
kickoff time in your time zone and a countdown to the next kickoff, plus the top
five and bottom five of the league table (worked out from every finished game).
While a matchweek is under way, games that have started show the live score and
finished games show the full-time score. The screen moves on to the following
matchweek once every game in it has finished.

Data comes from the Fantasy Premier League API (fixtures and team names), which
is free and needs no account or key.

    GET /football.json      panel data (what the recipe uses)

Auth: optional. Set FOOTBALL_BRIDGE_TOKEN to require `Authorization: Bearer ...`.

Environment:
    FOOTBALL_BRIDGE_TOKEN      shared secret LaraPaper must send (optional; fixtures aren't private)
    FOOTBALL_REFRESH_MINUTES   background refresh interval, default 15 (minimum 5)
    FOOTBALL_FAVORITE          a club's 3-letter code (e.g. ARS); its games are marked with a star
    FOOTBALL_TABLE_ROWS        how many table rows the screen shows, default 15: the bottom 4
                               always, the rest from 1st down, with "···" for the teams skipped
    FOOTBALL_CRESTS_DIR        folder of your own crest pictures, default /app/images/football
                               (football/images on the Pi, out of git). Name each file after the
                               club's code, e.g. ARS.png; clubs without one get a plain badge.
    FOOTBALL_LION_HEIGHT       px, default 30: the league logo (lion.png in the crests folder),
                               shown left of the title at the height of its capitals
    FOOTBALL_CREST_THRESHOLD   0-255, default 150: colors darker than this turn black in crests
    FOOTBALL_DEMO              "1" to serve made-up data (this weekend's dates) without the API
    TZ                         IANA time zone, default America/New_York
"""

from __future__ import annotations

import base64
import io
import logging
import os
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from common import check_token, flag, page_info, refresh_forever, setting

NAME = "football"
log = logging.getLogger("football")

TZ = ZoneInfo(os.getenv("TZ") or "America/New_York")
BRIDGE_TOKEN = setting("FOOTBALL", "BRIDGE_TOKEN")
REFRESH_MINUTES = max(5, int(setting("FOOTBALL", "REFRESH_MINUTES", "15") or 15))
FAVORITE = setting("FOOTBALL", "FAVORITE").strip().upper()
DEMO = flag("FOOTBALL", "DEMO")
TABLE_ROWS = max(8, int(setting("FOOTBALL", "TABLE_ROWS", "15") or 15))
CRESTS_DIR = Path(setting("FOOTBALL", "CRESTS_DIR", "/app/images/football"))
CREST_L, CREST_S = 26, 22  # px: fixture list, table

API = "https://fantasy.premierleague.com/api"
MAX_ROWS = 10  # what fits in the fixture list; more (a rare double matchweek) shows "+N more"


# ── Formatting helpers ──
def _clock(t: datetime) -> str:
    return t.strftime("%I:%M %p").lstrip("0")


def _countdown(delta: timedelta) -> str:
    mins = int(delta.total_seconds() // 60)
    if mins <= 0:
        return "Kicking off"
    d, rem = divmod(mins, 24 * 60)
    h, m = divmod(rem, 60)
    if d:
        return f"in {d} day{'s' if d != 1 else ''} {h} hr" if h else f"in {d} day{'s' if d != 1 else ''}"
    if h:
        return f"in {h} hr {m} min" if m else f"in {h} hr"
    return f"in {m} min"


# ── Crests: your own pictures, one per club, turned into 1-bit e-ink images ──
_crest_cache: dict[str, tuple[float, dict[str, str]]] = {}
CREST_EXTS = (".png", ".webp", ".jpg", ".jpeg", ".gif", ".bmp")


def _crest_file(code: str) -> Path | None:
    if not CRESTS_DIR.is_dir():
        return None
    for f in CRESTS_DIR.iterdir():
        if f.stem.upper() == code.upper() and f.suffix.lower() in CREST_EXTS and f.is_file():
            return f
    return None


def _eink_crest(path: Path, size: int) -> str:
    """Trim the picture, fit it in a size x size square and make it pure black and white.

    A hard threshold rather than dithering: at 22-26 px a dot pattern turns a crest into
    noise, while solid shapes keep its outline. Colors darker than mid-gray go black."""
    from PIL import Image, ImageOps

    im = ImageOps.exif_transpose(Image.open(path)).convert("RGBA")
    flat = Image.new("RGBA", im.size, (255, 255, 255, 255))
    flat.alpha_composite(im)
    gray = flat.convert("L")
    bbox = gray.point(lambda v: 255 if v < 245 else 0).getbbox()
    if bbox:
        gray = gray.crop(bbox)
    scale = min(size / gray.width, size / gray.height)
    gray = gray.resize((max(1, round(gray.width * scale)), max(1, round(gray.height * scale))), Image.Resampling.LANCZOS)
    canvas = Image.new("L", (size, size), 255)
    canvas.paste(gray, ((size - gray.width) // 2, (size - gray.height) // 2))
    bw = canvas.point(lambda v: 255 if v > CREST_THRESHOLD else 0).convert("1")
    buf = io.BytesIO()
    bw.save(buf, format="PNG", optimize=True)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


CREST_THRESHOLD = int(setting("FOOTBALL", "CREST_THRESHOLD", "150") or 150)


def crest(code: str) -> dict[str, str]:
    """{"crest": 26 px image, "crest_s": 22 px image} for a club, or {} without a picture.
    Converted once, and again only when the file changes."""
    path = _crest_file(code)
    if not path:
        _crest_cache.pop(code, None)
        return {}
    mtime = path.stat().st_mtime
    hit = _crest_cache.get(code)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        out = {"crest": _eink_crest(path, CREST_L), "crest_s": _eink_crest(path, CREST_S)}
        log.info("Loaded crest %s", path.name)
    except Exception as err:
        log.warning("Could not load crest %s: %s", path, err)
        out = {}
    _crest_cache[code] = (mtime, out)
    return out


LION_H = int(setting("FOOTBALL", "LION_HEIGHT", "30") or 30)  # px, the height of the title's capitals
_lion_cache: tuple[float, dict[str, Any] | None] | None = None


def lion() -> dict[str, Any] | None:
    """The league logo for the title (lion.png in the crests folder), LION_H px tall, or None."""
    global _lion_cache
    path = _crest_file("lion")
    if not path:
        _lion_cache = None
        return None
    mtime = path.stat().st_mtime
    if _lion_cache and _lion_cache[0] == mtime:
        return _lion_cache[1]
    out = None
    try:
        from PIL import Image

        im = Image.open(path).convert("RGBA")
        flat = Image.new("RGBA", im.size, (255, 255, 255, 255))
        flat.alpha_composite(im)
        gray = flat.convert("L")
        bbox = gray.point(lambda v: 255 if v < 245 else 0).getbbox()
        if bbox:
            gray = gray.crop(bbox)
        w = max(1, round(gray.width * LION_H / gray.height))
        gray = gray.resize((w, LION_H), Image.Resampling.BOX)
        bw = gray.point(lambda v: 255 if v > CREST_THRESHOLD else 0).convert("1")
        buf = io.BytesIO()
        bw.save(buf, format="PNG", optimize=True)
        out = {"src": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode(), "w": w, "h": LION_H}
        log.info("Loaded league logo %s", path.name)
    except Exception as err:
        log.warning("Could not load league logo %s: %s", path, err)
    _lion_cache = (mtime, out)
    return out


def _team(t: dict[str, Any]) -> dict[str, Any]:
    code = t["code"]
    return {"code": code, "name": t["name"], "fav": bool(FAVORITE) and code == FAVORITE, **crest(code)}


# ── Building the panel ──
def pick_matchweek(fixtures: list[dict[str, Any]]) -> tuple[int | None, list[dict[str, Any]]]:
    """The earliest matchweek with a game not yet finished, and its dated fixtures."""
    dated = [f for f in fixtures if f.get("event") and f.get("kickoff_time")]
    open_ = [f for f in dated if not (f.get("finished") or f.get("finished_provisional"))]
    if not open_:
        return None, []
    event = min(open_, key=lambda f: f["kickoff_time"])["event"]
    games = sorted((f for f in dated if f["event"] == event), key=lambda f: (f["kickoff_time"], f["id"]))
    return event, games


def build_panel(event: int | None, games: list[dict[str, Any]], teams: dict[int, dict[str, Any]], now: datetime) -> dict[str, Any]:
    panel: dict[str, Any] = {"ok": True, "as_of": _clock(now), "demo": DEMO, "lion": lion()}
    if not games:
        panel.update(no_games=True, title="Premier League")
        return panel

    rows: list[dict[str, Any]] = []
    for f in games:
        ko = datetime.fromisoformat(f["kickoff_time"].replace("Z", "+00:00")).astimezone(TZ)
        done = bool(f.get("finished") or f.get("finished_provisional"))
        live = bool(f.get("started")) and not done
        hs, as_ = f.get("team_h_score"), f.get("team_a_score")
        score = f"{hs}–{as_}" if hs is not None and as_ is not None else ""
        state = "ft" if done else "live" if live else "up"
        home, away = _team(teams[f["team_h"]]), _team(teams[f["team_a"]])
        rows.append({
            "ko": ko, "state": state, "home": home, "away": away,
            "time": _clock(ko), "score": score if state != "up" else "",
            "minute": f"{f['minutes']}′" if live and f.get("minutes") else "LIVE",
            "fav": home["fav"] or away["fav"],
        })

    # Group the list by day; a kickoff time is printed once per run of games at that time.
    days: list[dict[str, Any]] = []
    shown = rows[:MAX_ROWS]
    for r in shown:
        key = r["ko"].date()
        if not days or days[-1]["key"] != key:
            days.append({"key": key, "label": f"{r['ko']:%A}", "date": f"{r['ko']:%b} {r['ko'].day}", "count": 0, "games": []})
        day = days[-1]
        prev = day["games"][-1] if day["games"] else None
        r["repeat"] = bool(prev and prev["state"] == "up" and r["state"] == "up" and prev["time"] == r["time"])
        day["games"].append(r)
    for d in days:
        d["count"] = sum(1 for r in rows if r["ko"].date() == d["key"])

    # The line under the title: games in play, otherwise the next kickoff and a countdown.
    live = [r for r in rows if r["state"] == "live"]
    upcoming = [r for r in rows if r["state"] == "up"]
    if live:
        status = {"tag": "LIVE", "text": f"{len(live)} game{'s' if len(live) != 1 else ''} in play"}
    elif upcoming:
        first = upcoming[0]
        n = sum(1 for r in upcoming if r["ko"] == first["ko"])
        status = {"tag": _countdown(first["ko"] - now).upper(),
                  "text": f"Next kickoff {first['ko']:%a} {_clock(first['ko'])}" + (f" · {n} games" if n > 1 else "")}
    else:
        status = {"tag": "", "text": "All games played"}

    first_day, last_day = rows[0]["ko"], rows[-1]["ko"]
    span = f"{first_day:%a} {first_day:%b} {first_day.day}"
    if last_day.date() != first_day.date():
        span += f" – {last_day:%a} {last_day:%b} {last_day.day}" if last_day.month != first_day.month else f" – {last_day:%a} {last_day.day}"
    played = sum(1 for r in rows if r["state"] == "ft")

    for r in rows:
        r.pop("ko", None)
    for d in days:
        d.pop("key", None)
    panel.update(
        title=f"Matchweek {event}" if event else "Next matchweek",
        span=span,
        summary=f"{len(rows)} games" + (f" · {played} played" if played else ""),
        status=status,
        days=days,
        more=max(0, len(rows) - len(shown)),
    )
    return panel


def build_table(fixtures: list[dict[str, Any]], teams: dict[int, dict[str, Any]]) -> dict[str, Any]:
    """League table from every finished game: points, then goal difference, then goals scored."""
    t = {tid: {"p": 0, "w": 0, "d": 0, "l": 0, "gf": 0, "ga": 0} for tid in teams}
    for f in fixtures:
        if not (f.get("finished") or f.get("finished_provisional")):
            continue
        hs, as_ = f.get("team_h_score"), f.get("team_a_score")
        if hs is None or as_ is None:
            continue
        for tid, gf, ga in ((f["team_h"], hs, as_), (f["team_a"], as_, hs)):
            row = t.setdefault(tid, {"p": 0, "w": 0, "d": 0, "l": 0, "gf": 0, "ga": 0})
            row["p"] += 1
            row["gf"] += gf
            row["ga"] += ga
            row["w" if gf > ga else "d" if gf == ga else "l"] += 1
    order = sorted(t, key=lambda tid: (-(3 * t[tid]["w"] + t[tid]["d"]), -(t[tid]["gf"] - t[tid]["ga"]), -t[tid]["gf"], teams.get(tid, {}).get("name", "")))
    rows = []
    for pos, tid in enumerate(order, 1):
        r = t[tid]
        gd = r["gf"] - r["ga"]
        rows.append({"pos": pos, **_team(teams[tid]), "p": r["p"], "gd": f"+{gd}" if gd > 0 else str(gd).replace("-", "−"),
                     "pts": 3 * r["w"] + r["d"]})
    # The bottom 4 always, then as many as fit from 1st down; "···" marks the teams skipped.
    n = len(rows)
    bottom_n = min(4, n)
    top_n = max(0, min(TABLE_ROWS, n) - bottom_n)
    gap = top_n + bottom_n < n
    top, bottom = (rows[:top_n], rows[n - bottom_n:]) if gap else (rows, [])
    played = max((r["p"] for r in rows), default=0)
    return {"top": top, "bottom": bottom, "gap": gap, "drop_from": n - 2, "count": n, "played": played}


# ── Demo data: one matchweek, dated this coming weekend ──
DEMO_TEAMS = {
    1: ("ARS", "Arsenal"), 2: ("AVL", "Aston Villa"), 3: ("BOU", "Bournemouth"), 4: ("BRE", "Brentford"),
    5: ("BHA", "Brighton"), 6: ("CHE", "Chelsea"), 7: ("COV", "Coventry"), 8: ("CRY", "Crystal Palace"),
    9: ("EVE", "Everton"), 10: ("FUL", "Fulham"), 11: ("HUL", "Hull City"), 12: ("IPS", "Ipswich"),
    13: ("LEE", "Leeds"), 14: ("LIV", "Liverpool"), 15: ("MCI", "Man City"), 16: ("MUN", "Man Utd"),
    17: ("NEW", "Newcastle"), 18: ("NFO", "Nott'm Forest"), 19: ("SUN", "Sunderland"), 20: ("TOT", "Spurs"),
}
# (day offset from Saturday, UK kickoff hour:minute, home, away)
DEMO_GAMES = [
    (0, "12:30", 15, 10), (0, "15:00", 2, 19), (0, "15:00", 3, 18), (0, "15:00", 6, 13), (0, "15:00", 7, 8),
    (0, "15:00", 20, 12), (0, "17:30", 17, 1), (1, "14:00", 11, 5), (1, "16:30", 14, 16), (2, "20:00", 4, 9),
]


def _demo(now: datetime) -> tuple[int, list[dict[str, Any]], dict[int, dict[str, Any]], list[dict[str, Any]]]:
    uk = ZoneInfo("Europe/London")
    today = now.astimezone(uk).date()
    sat = today + timedelta(days=(5 - today.weekday()) % 7 or 7)
    games = []
    for i, (off, hm, h, a) in enumerate(DEMO_GAMES):
        hh, mm = map(int, hm.split(":"))
        ko = datetime(sat.year, sat.month, sat.day, hh, mm, tzinfo=uk) + timedelta(days=off)
        games.append({"id": i, "event": 12, "kickoff_time": ko.astimezone(timezone.utc).isoformat(),
                      "team_h": h, "team_a": a, "started": False, "finished": False})
    teams = {k: {"code": c, "name": n} for k, (c, n) in DEMO_TEAMS.items()}
    # Eleven made-up rounds of results for the table.
    rng = random.Random(11)
    strength = {k: rng.random() for k in teams}
    history = []
    for rnd in range(11):
        ids = list(teams)
        rng.shuffle(ids)
        for h, a in zip(ids[::2], ids[1::2]):
            edge = strength[h] - strength[a] + 0.15
            hs = max(0, round(rng.gauss(1.4 + edge, 1)))
            as_ = max(0, round(rng.gauss(1.1 - edge, 1)))
            history.append({"team_h": h, "team_a": a, "team_h_score": hs, "team_a_score": as_, "finished": True})
    return 12, games, teams, history


# ── Live data ──
class Source:
    def __init__(self) -> None:
        self.panel: dict[str, Any] | None = None
        self.error: str | None = None
        self.last_ok: datetime | None = None
        self._teams: dict[int, dict[str, Any]] = {}
        self._teams_at: datetime | None = None

    async def refresh(self) -> None:
        now = datetime.now(TZ)
        if DEMO:
            event, games, teams, history = _demo(now)
            self._teams = teams
            self.panel = {**build_panel(event, games, teams, now), "table": build_table(history, teams)}
            self.error, self.last_ok = None, now
            return

        headers = {"User-Agent": "reterminal-e1001 (personal e-paper display)"}
        async with httpx.AsyncClient(timeout=30, headers=headers, follow_redirects=True) as client:
            # Team names change once a season; fetch them daily.
            if not self._teams or not self._teams_at or now - self._teams_at > timedelta(hours=24):
                r = await client.get(f"{API}/bootstrap-static/")
                r.raise_for_status()
                self._teams = {t["id"]: {"code": t["short_name"], "name": t["name"]} for t in r.json()["teams"]}
                self._teams_at = now
            r = await client.get(f"{API}/fixtures/")
            r.raise_for_status()
            fixtures = r.json()

        event, games = pick_matchweek(fixtures)
        self.panel = {**build_panel(event, games, self._teams, now), "table": build_table(fixtures, self._teams)}
        self.error, self.last_ok = None, now
        log.info("Refreshed: matchweek %s, %d games", event, len(games))


source = Source()
router = APIRouter()


def start() -> list:
    return [refresh_forever(source, REFRESH_MINUTES, log)]


def health() -> dict[str, Any]:
    codes = [c for c in sorted(t["code"] for t in source._teams.values()) if c.upper() != "LION"] if source._teams else []
    return {
        "ok": source.error is None and source.panel is not None,
        "demo": DEMO,
        "crests_missing": [c for c in codes if not _crest_file(c)],
        "last_ok": source.last_ok.isoformat() if source.last_ok else None,
        "error": source.error,
    }


@router.get("/football.json")
async def football_json(request: Request) -> JSONResponse:
    check_token(request, BRIDGE_TOKEN)
    if source.panel:
        out = dict(source.panel)
        if source.error:
            out["warning"] = "Couldn't reach the fixtures feed; showing data from " + out.get("as_of", "earlier")
        return JSONResponse({**out, **page_info(NAME)})
    return JSONResponse({"ok": False, "error": source.error or "Starting up…", **page_info(NAME)})
