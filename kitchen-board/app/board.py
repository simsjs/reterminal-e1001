"""Builds the data for each screen and renders it to HTML."""
import os
from datetime import date, datetime, timedelta

import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape

import calendar_data
import config
import weather

_env = Environment(
    loader=FileSystemLoader(os.path.join(os.path.dirname(__file__), "templates")),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)

PAGES = ["brief", "week"]

# Values LaraPaper fills in from the device (see recipes/*.liquid).
PLACEHOLDER_PREVIEW = {
    "%%BATTERY%%": "86",
    "%%BATTERY_FILL%%": "17",
    "%%WIFI_CLASS%%": "wifi-3",
    "%%IN_TEMP%%": "68°F",
    "%%IN_RH%%": "41%",
}


def now() -> datetime:
    if config.NOW_OVERRIDE:
        return datetime.fromisoformat(config.NOW_OVERRIDE).replace(tzinfo=config.TZ)
    return datetime.now(config.TZ)


def load_content() -> dict:
    try:
        with open(config.CONTENT_PATH, encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except FileNotFoundError:
        return {}


def clock(dt: datetime) -> str:
    return f"{dt.hour % 12 or 12}:{dt.minute:02d}"


def clock_ampm(dt: datetime) -> str:
    return f"{clock(dt)} {'AM' if dt.hour < 12 else 'PM'}"


def _common(page: str, n: datetime, content: dict) -> dict:
    return {
        "page": page,
        "page_index": PAGES.index(page),
        "page_count": len(PAGES),
        "board_name": str(content.get("board_name", "Kitchen")).upper(),
        "updated": clock_ampm(n),
        "font_base": config.FONT_BASE_URL,
        "sample": calendar_data.using_sample(),
    }


def build_brief(n: datetime) -> dict:
    content = load_content()
    today = n.date()
    events = calendar_data.events_between(today, today + timedelta(days=1), include_brief_only=True)

    rows, next_marked = [], False
    for e in events:
        done = e.end is not None and e.end < n
        is_next = False
        if not next_marked and e.start is not None and e.start >= n:
            is_next = next_marked = True
        rows.append({"time": clock(e.start) if e.start else "", "title": e.title,
                     "done": done, "next": is_next, "all_day": e.all_day})

    total = len(rows)
    while len(rows) > 5 and rows[0]["done"]:   # drop finished items first
        rows.pop(0)
    rows = rows[:5]

    tiles = (content.get("tiles") or [])[:3]
    return {
        **_common("brief", n, content),
        "title": "MORNING BRIEF",
        "weekday": n.strftime("%A").upper(),
        "date_label": f"{n.strftime('%b')} {n.day}",
        "weather": weather.get_weather(n),
        "rows": rows,
        "total": total,
        "tiles": tiles,
    }


def _countdown(content: dict, today: date) -> str:
    best = None
    for item in content.get("countdowns") or []:
        try:
            d = item["date"] if isinstance(item["date"], date) else date.fromisoformat(str(item["date"]))
        except (KeyError, ValueError):
            continue
        days = (d - today).days
        if 0 <= days <= int(item.get("show_within_days", 60)) and (best is None or days < best[0]):
            best = (days, str(item.get("name", "")).upper())
    if best is None:
        return ""
    days, name = best
    if days == 0:
        return f"{name} TODAY"
    if days == 1:
        return f"{name} TOMORROW"
    return f"{name} IN {days} DAYS"


def build_week(n: datetime) -> dict:
    content = load_content()
    today = n.date()
    monday = today - timedelta(days=today.weekday())
    events = calendar_data.events_between(monday, monday + timedelta(days=7), include_brief_only=False)

    days = []
    for i in range(7):
        d = monday + timedelta(days=i)
        items = []
        for e in (ev for ev in events if ev.day == d):
            if d < today:
                done = True
            elif d == today:
                done = e.end is not None and e.end < n
            else:
                done = False
            items.append({"time": clock(e.start) if e.start else "", "title": e.title, "done": done})
        days.append({
            "dow": "TODAY" if d == today else d.strftime("%a").upper(),
            "num": d.day,
            "today": d == today,
            "items": items,
        })

    reminders = [str(r) for r in (content.get("reminders") or [])][:4]
    return {
        **_common("week", n, content),
        "title": "THIS WEEK",
        "week_label": f"Week of {monday.strftime('%b')} {monday.day}",
        "countdown": _countdown(content, today),
        "days": days,
        "reminders": reminders,
    }


BUILDERS = {"brief": build_brief, "week": build_week}


def render(page: str, preview: bool = False) -> str:
    ctx = BUILDERS[page](now())
    html = _env.get_template(f"{page}.html").render(**ctx)
    if preview:
        for key, value in PLACEHOLDER_PREVIEW.items():
            html = html.replace(key, value)
    return html


def render_fragment(page: str) -> str:
    """The markup LaraPaper injects into its recipe (placeholders left in)."""
    return render(page, preview=False)
