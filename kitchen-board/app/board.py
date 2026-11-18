"""Builds the data for each screen and renders it to HTML."""
import os
from datetime import date, datetime, timedelta

import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape

import calendar_data
import config
import tasks_data

_env = Environment(
    loader=FileSystemLoader(os.path.join(os.path.dirname(__file__), "templates")),
    autoescape=select_autoescape(["html"]),
    trim_blocks=True,
    lstrip_blocks=True,
)

def _dot_pages() -> list[str]:
    """Screens that get a header dot, in playlist order (SCREEN_ORDER)."""
    return config.SCREEN_ORDER

# Values LaraPaper fills in from the device (see recipes/*.liquid).
PLACEHOLDER_PREVIEW = {
    "%%BATTERY%%": "86",
    "%%BATTERY_FILL%%": "17",
    "%%WIFI_CLASS%%": "wifi-3",
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
        "page_index": _dot_pages().index(page) if page in _dot_pages() else -1,
        "page_count": len(_dot_pages()),
        "board_name": str(content.get("board_name") or "").upper(),
        "updated": clock_ampm(n),
        "font_base": config.FONT_BASE_URL,
        "sample": calendar_data.using_sample(),
    }


def _countdown(content: dict, today: date) -> str:
    """The nearest upcoming holiday (from the holiday calendar) or personal
    countdown (from content.yml), whichever is sooner."""
    candidates = []   # (days away, name)
    window = config.HOLIDAY_SHOW_WITHIN_DAYS
    for d, name in calendar_data.holidays(today, today + timedelta(days=window + 1)) or []:
        candidates.append(((d - today).days, name))
    for item in content.get("countdowns") or []:
        try:
            d = item["date"] if isinstance(item["date"], date) else date.fromisoformat(str(item["date"]))
        except (KeyError, ValueError, TypeError):
            continue
        days = (d - today).days
        if 0 <= days <= int(item.get("show_within_days", window)):
            candidates.append((days, str(item.get("name", ""))))
    if not candidates:
        return ""
    days, name = min(candidates, key=lambda c: c[0])
    name = name.upper()
    if days == 0:
        return f"{name} TODAY"
    if days == 1:
        return f"{name} TOMORROW"
    return f"{name} IN {days} DAYS"


# This Week shows yesterday, today and the next three days.
WEEK_DAYS_BEFORE = 1
WEEK_DAYS_AFTER = 3


def _range_label(first: date, last: date) -> str:
    if first.month == last.month:
        return f"{first.strftime('%b')} {first.day} – {last.day}"
    return f"{first.strftime('%b')} {first.day} – {last.strftime('%b')} {last.day}"


def build_week(n: datetime) -> dict:
    content = load_content()
    today = n.date()
    first = today - timedelta(days=WEEK_DAYS_BEFORE)
    count = WEEK_DAYS_BEFORE + 1 + WEEK_DAYS_AFTER
    events = calendar_data.events_between(first, first + timedelta(days=count))

    days = []
    for i in range(count):
        d = first + timedelta(days=i)
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

    reminders = tasks_data.reminders(today)
    if reminders is None:   # Google Tasks not set up: use the typed list
        reminders = [{"title": str(r), "when": "", "overdue": False}
                     for r in (content.get("reminders") or [])]
    more = max(0, len(reminders) - 4)
    reminders = reminders[:4]
    return {
        **_common("week", n, content),
        "title": "THIS WEEK",
        "week_label": _range_label(first, first + timedelta(days=count - 1)),
        "countdown": _countdown(content, today),
        "days": days,
        "reminders": reminders,
        "reminders_more": more,
    }


BUILDERS = {"week": build_week}


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
