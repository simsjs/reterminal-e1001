"""Settings for the kitchen board, read from environment variables."""
import os
from zoneinfo import ZoneInfo


def _split(value: str) -> list[str]:
    parts = value.replace("\n", ",").split(",")
    return [p.strip() for p in parts if p.strip()]


TZ_NAME = os.environ.get("TZ", "America/New_York")
TZ = ZoneInfo(TZ_NAME)

# Weather location (defaults to Fairfax, VA). Open-Meteo needs no API key.
WEATHER_LAT = float(os.environ.get("WEATHER_LAT", "38.8462"))
WEATHER_LON = float(os.environ.get("WEATHER_LON", "-77.3064"))

# Private iCal (.ics) links. CALENDAR_ICS_URLS feed both screens;
# BRIEF_ONLY_ICS_URLS (e.g. a work calendar) only feed the morning brief.
CALENDAR_ICS_URLS = _split(os.environ.get("CALENDAR_ICS_URLS", ""))
BRIEF_ONLY_ICS_URLS = _split(os.environ.get("BRIEF_ONLY_ICS_URLS", ""))

# "auto": sample events until a calendar is configured; "on": all sample data
# (weather too); "off": never use sample data.
SAMPLE_DATA = os.environ.get("SAMPLE_DATA", "auto").lower()

# Where the renderer (LaraPaper's headless Chrome) fetches the fonts from.
FONT_BASE_URL = os.environ.get("FONT_BASE_URL", "http://kitchen-board:8080").rstrip("/")

CONTENT_PATH = os.environ.get(
    "CONTENT_PATH", os.path.join(os.path.dirname(__file__), "..", "content", "content.yml")
)

PORT = int(os.environ.get("PORT", "8080"))

# How many page dots the header shows. The kitchen screens are dots 1 and 2;
# raise this when the playlist has more screens (e.g. 3 with the Polestar one).
# 0 = just the kitchen screens.
PAGE_COUNT = int(os.environ.get("PAGE_COUNT", "0") or 0)

# Pin "now" for testing, e.g. NOW=2026-11-12T06:02
NOW_OVERRIDE = os.environ.get("NOW", "").strip()

CACHE_SECONDS = int(os.environ.get("CACHE_SECONDS", "600"))
