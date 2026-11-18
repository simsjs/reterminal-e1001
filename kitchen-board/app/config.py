"""Settings for the calendar screens, read from environment variables."""
import os
from zoneinfo import ZoneInfo


def _split(value: str) -> list[str]:
    parts = value.replace("\n", ",").split(",")
    return [p.strip() for p in parts if p.strip()]


TZ_NAME = os.environ.get("TZ", "America/New_York")
TZ = ZoneInfo(TZ_NAME)

# Private iCal (.ics) links shown on This Week.
CALENDAR_ICS_URLS = _split(os.environ.get("CALENDAR_ICS_URLS", ""))

# Holiday countdown on This Week: Google's public "Holidays in United States"
# calendar. Set it empty to use only the countdowns list in content.yml.
HOLIDAYS_ICS_URL = os.environ.get(
    "HOLIDAYS_ICS_URL",
    "https://calendar.google.com/calendar/ical/"
    "en.usa.official%23holiday%40group.v.calendar.google.com/public/basic.ics",
).strip()
# Only count down to a holiday this many days out.
HOLIDAY_SHOW_WITHIN_DAYS = int(os.environ.get("HOLIDAY_SHOW_WITHIN_DAYS", "60") or 60)

# Reminders on This Week come from Google Tasks (the "Tasks" entry in Google
# Calendar). Get these three values with tools/get_tasks_token.py. Until they're
# set, the reminders strip shows the list in content.yml.
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
GOOGLE_TASKS_REFRESH_TOKEN = os.environ.get("GOOGLE_TASKS_REFRESH_TOKEN", "").strip()
# Which task lists to read, by name (comma-separated). Empty = all lists.
TASKS_LISTS = _split(os.environ.get("TASKS_LISTS", ""))
# Show tasks due within this many days (overdue ones always show).
TASKS_DAYS_AHEAD = int(os.environ.get("TASKS_DAYS_AHEAD", "7") or 7)
# Also show tasks with no due date, after the dated ones.
TASKS_INCLUDE_UNDATED = os.environ.get("TASKS_INCLUDE_UNDATED", "off").lower() in ("on", "1", "true", "yes")

# "auto": sample events until a calendar is configured; "on": all sample data
# (holidays too); "off": never use sample data.
SAMPLE_DATA = os.environ.get("SAMPLE_DATA", "auto").lower()

# Where the renderer (LaraPaper's headless Chrome) fetches the fonts from.
FONT_BASE_URL = os.environ.get("FONT_BASE_URL", "http://kitchen-board:8080").rstrip("/")

CONTENT_PATH = os.environ.get(
    "CONTENT_PATH", os.path.join(os.path.dirname(__file__), "..", "content", "content.yml")
)

PORT = int(os.environ.get("PORT", "8080"))

# The screens in playlist order, which sets the header dots: one dot per
# screen, with this screen's dot filled. Same setting for every service.
# Names: week, polestar, energy, fitness, weather.
SCREEN_ORDER = [s.lower() for s in _split(os.environ.get("SCREEN_ORDER", ""))] or ["week"]

# Pin "now" for testing, e.g. NOW=2026-11-12T06:02
NOW_OVERRIDE = os.environ.get("NOW", "").strip()

CACHE_SECONDS = int(os.environ.get("CACHE_SECONDS", "600"))
