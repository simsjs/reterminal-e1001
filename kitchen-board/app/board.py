"""Builds the data for each screen and renders it to HTML."""
import os
from datetime import date, datetime, timedelta

import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape

import calendar_data
import config
import flourish
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


def _next_occasion(content: dict, today: date) -> tuple[int, str] | None:
    """(days away, name) of the nearest upcoming holiday (from the holiday
    calendar) or personal countdown (from content.yml), whichever is sooner."""
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
        return None
    return min(candidates, key=lambda c: c[0])


def _countdown(occasion: tuple[int, str] | None) -> str:
    if not occasion:
        return ""
    days, name = occasion[0], occasion[1].upper()
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


# Column fitting. These mirror .col-body / .ev / .ev-time / .ev-title / .more in
# _style.css; keep them in step when the sizes there change.
COL_BODY_PX = 218        # 286 inner grid - 59 header - 9 top padding
EV_TIME_PX = 16          # 14px * 1.15
EV_LINE_PX = 19          # 16px * 1.2
EV_GAP_PX = 11
EV_MAX_LINES = 3         # -webkit-line-clamp
MORE_PX = 14
LINE_CHARS = 16          # a little under what fits, so we drop to "+N MORE" rather than clip


def _title_lines(title: str) -> int:
    """Rough greedy word wrap of an event title, capped at the line clamp."""
    lines, cur = 0, LINE_CHARS          # start "full" so the first word opens line 1
    for word in title.split():
        if cur + 1 + len(word) <= LINE_CHARS:
            cur += 1 + len(word)
        else:                            # new line; an over-long word breaks across lines
            lines += -(-len(word) // LINE_CHARS)
            cur = (len(word) - 1) % LINE_CHARS + 1
    return min(max(lines, 1), EV_MAX_LINES)


def _fit_items(items: list[dict]) -> tuple[list[dict], int]:
    """The events that fit in a day column, and how many are left over."""
    heights = [(EV_TIME_PX if e["time"] else 0) + EV_LINE_PX * _title_lines(e["title"]) for e in items]
    used, shown = 0, 0
    for i, h in enumerate(heights):
        need = used + (EV_GAP_PX if i else 0) + h
        rest = len(items) - (i + 1)
        if need + (EV_GAP_PX + MORE_PX if rest else 0) > COL_BODY_PX:
            break
        used, shown = need, i + 1
    return items[:shown], len(items) - shown


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
        shown, more = _fit_items(items)
        days.append({
            "dow": "TODAY" if d == today else d.strftime("%a").upper(),
            "num": d.day,
            "today": d == today,
            "items": shown,
            "more": more,
        })

    occasion = _next_occasion(content, today)
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
        "countdown": _countdown(occasion),
        "flourish": flourish.art(flourish.theme_for(occasion[1]) if occasion else None),
        "days": days,
        "reminders": reminders,
        "reminders_more": more,
        "tasks_offline": tasks_data.offline(),
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
