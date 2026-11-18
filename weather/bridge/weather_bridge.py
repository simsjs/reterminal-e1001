"""
Weather -> LaraPaper bridge for the Weather screen.

Pulls the forecast from Open-Meteo (free, no API key), the US air quality index from
Open-Meteo's air-quality API, and active watches and warnings from the National Weather
Service. Refreshes in the background and serves a small, pre-formatted JSON document
that a LaraPaper (TRMNL BYOS) recipe polls.

    GET /weather.json       panel data (what the recipe uses)
    GET /health

Auth: if BRIDGE_TOKEN is set, send `Authorization: Bearer <BRIDGE_TOKEN>` (or ?token=...).
Weather isn't private, so the token is optional. /health is always open.

The screen shows:

    left     now: icon, temperature, feels-like, today's high and low, a one-line plan
             (or the most serious NWS alert), then wind, humidity, UV, air quality,
             sunrise and sunset, and today's rain or snow
    right    the next 24 hours (temperature line, chance-of-rain bars, an icon every
             3 hours) and the next 7 days

With scene pictures in SCENE_DIR, the left side becomes a picture of the house that
matches the season and the weather (snowing, snow on the ground, rain, windy, clear,
cloudy, night), with the current conditions underneath. See weather/images/README.md.

Environment:
    WEATHER_LAT, WEATHER_LON   location, default Fairfax, VA
    WEATHER_PLACE              name shown on the screen, default Fairfax
    BRIDGE_TOKEN               optional shared secret LaraPaper sends
    REFRESH_MINUTES            background refresh interval, default 15 (minimum 5)
    ALERTS                     "off" to skip National Weather Service alerts
    TZ                         IANA time zone, default America/New_York
    SCENE_DIR                  folder of scene pictures, default /app/images
    SCENE                      "auto" (default) picks a picture from the weather, "off" never
                               shows one, or a picture name (e.g. "rain") always shows that one
    SCENE_FOCUS                0-1, which part of a tall picture to keep (0 = top), default 0.45
    DEMO                       "1" to serve made-up data without going online
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import math
import os
import random
import secrets
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

log = logging.getLogger("weather-bridge")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")

TZ_NAME = os.getenv("TZ") or "America/New_York"
TZ = ZoneInfo(TZ_NAME)
LAT = float(os.getenv("WEATHER_LAT") or 38.8462)
LON = float(os.getenv("WEATHER_LON") or -77.3064)
PLACE = (os.getenv("WEATHER_PLACE") or "Fairfax").strip()
BRIDGE_TOKEN = os.getenv("BRIDGE_TOKEN", "")
REFRESH_MINUTES = max(5, int(os.getenv("REFRESH_MINUTES", "15") or 15))
ALERTS = os.getenv("ALERTS", "on").strip().lower() not in ("0", "off", "no", "false")
DEMO = os.getenv("DEMO", "0").strip().lower() in ("1", "true", "yes", "on")
SCENE_DIR = os.getenv("SCENE_DIR", "/app/images")
SCENE_MODE = (os.getenv("SCENE") or "auto").strip().lower()

USER_AGENT = "reterminal-e1001 weather-bridge (github.com/simsjs/reterminal-e1001)"

# Pixel sizes the recipe draws with (full layout). Kept here so the template stays simple.
HOURS = 24
CHART_W, CHART_H = 456, 116  # next-24-hours chart
SCENE_CHART_W = 300          # the chart is narrower when the left side is a scene picture
SCENE_W, SCENE_H = 460, 220  # scene picture; the screen crops it to fit, never rescales
TEMP_TOP, TEMP_BOTTOM = 22, 30  # room above the line for labels, below it for the rain bars to show
DAYS = 7

# ── Weather codes (WMO, as Open-Meteo sends them) ──
SNOW = {71, 73, 75, 77, 85, 86}
STORM = {95, 96, 99}
ICE = {56, 57, 66, 67}
RAIN = {51, 53, 55, 61, 63, 65, 80, 81, 82} | ICE
FOG = {45, 48}

CONDITION = {
    0: "Clear", 1: "Mostly clear", 2: "Partly cloudy", 3: "Cloudy",
    45: "Fog", 48: "Freezing fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle", 56: "Freezing drizzle", 57: "Freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain", 67: "Freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains",
    80: "Showers", 81: "Showers", 82: "Heavy showers", 85: "Snow showers", 86: "Heavy snow showers",
    95: "Thunderstorms", 96: "Storms with hail", 99: "Storms with hail",
}


def icon_for(code: int, day: bool = True) -> str:
    """The recipe's icon names: sun, moon, partly, partly-night, cloud, rain, sleet, snow, storm, fog."""
    if code in STORM:
        return "storm"
    if code in SNOW:
        return "snow"
    if code in ICE:
        return "sleet"
    if code in RAIN:
        return "rain"
    if code in FOG:
        return "fog"
    if code == 0:
        return "sun" if day else "moon"
    if code in (1, 2):
        return "partly" if day else "partly-night"
    return "cloud"


def condition(code: int, day: bool = True) -> str:
    text = CONDITION.get(code, "Cloudy")
    if day and code in (0, 1):
        text = text.replace("clear", "sunny").replace("Clear", "Sunny")
    return text


def precip_kind(code: int | None) -> str:
    if code in SNOW:
        return "Snow"
    if code in STORM:
        return "Storms"
    if code in ICE:
        return "Freezing rain"
    if code in (51, 53, 55):
        return "Drizzle"
    return "Rain"


# ── Formatting helpers ──
def _clock(t: datetime) -> str:
    return t.strftime("%I:%M %p").lstrip("0")


def _short_clock(t: datetime) -> str:
    """6:56 for sunrise and sunset; AM/PM is obvious."""
    return t.strftime("%I:%M").lstrip("0")


def _hour(t: datetime) -> str:
    """4 PM"""
    return f"{t.hour % 12 or 12} {'AM' if t.hour < 12 else 'PM'}"


def _axis_hour(t: datetime) -> str:
    """4P, NOON"""
    if t.hour == 12:
        return "NOON"
    return f"{t.hour % 12 or 12}{'A' if t.hour < 12 else 'P'}"


def _local(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=TZ)


def compass(deg: float | None) -> str:
    if deg is None:
        return ""
    return ("N", "NE", "E", "SE", "S", "SW", "W", "NW")[round(deg / 45) % 8]


def uv_level(uv: float) -> str:
    uv = round(uv)
    if uv <= 2:
        return "Low"
    if uv <= 5:
        return "Moderate"
    if uv <= 7:
        return "High"
    if uv <= 10:
        return "Very high"
    return "Extreme"


def aqi_level(aqi: float) -> str:
    if aqi <= 50:
        return "Good"
    if aqi <= 100:
        return "Moderate"
    if aqi <= 150:
        return "Unhealthy for some"
    if aqi <= 200:
        return "Unhealthy"
    if aqi <= 300:
        return "Very unhealthy"
    return "Hazardous"


def _inches(v: float) -> str:
    if v <= 0:
        return "0"
    if v < 0.01:
        return "<0.01"
    return f"{v:.2f}" if v < 1 else f"{v:.1f}"


def _deg(v: float | None) -> str:
    return f"{round(v)}°" if v is not None else "--"


# ── The panel ──
def hourly_window(fc: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    h = fc["hourly"]
    start = now.replace(minute=0, second=0, microsecond=0)
    rows = []
    for i, t in enumerate(h["time"]):
        ht = _local(t)
        if ht < start:
            continue
        rows.append(
            {
                "t": ht,
                "temp": h["temperature_2m"][i],
                "prob": h["precipitation_probability"][i] or 0,
                "precip": h["precipitation"][i] or 0,
                "code": h["weather_code"][i],
                "day": bool(h["is_day"][i]),
                "gust": (h.get("wind_gusts_10m") or [None] * len(h["time"]))[i],
                "snow": (h.get("snowfall") or [0] * len(h["time"]))[i] or 0,
            }
        )
        if len(rows) == HOURS:
            break
    return rows


def plan_text(hours: list[dict[str, Any]], today: dict[str, Any], now: datetime) -> str:
    """A short plan for the next 12 hours, in the calendar screens' voice."""
    ahead = hours[:12]
    parts = []

    wet = next((i for i, r in enumerate(ahead) if r["prob"] >= 50), None)
    if wet is not None:
        start = ahead[wet]
        kind = precip_kind(start["code"])
        end = next((r for r in hours[wet + 1 :] if r["prob"] < 30), None)
        if wet == 0:
            parts.append(f"{kind} now, easing by {_hour(end['t'])}." if end and end["t"] - now < timedelta(hours=6) else f"{kind} this hour.")
        elif end and end["t"] - start["t"] <= timedelta(hours=6):
            a, b = _hour(start["t"]), _hour(end["t"])
            if a[-2:] == b[-2:]:  # "3–9 PM", not "3 PM–9 PM"
                a = a[:-3]
            parts.append(f"{kind} {a}–{b}.")
        else:
            parts.append(f"{kind} after {_hour(start['t'])}.")
        if kind == "Snow":
            parts.append("Watch the roads.")
        elif kind in ("Storms", "Freezing rain"):
            parts.append("Take care out there.")
        else:
            parts.append("Bring an umbrella." if today["low"] >= 50 else "Grab a jacket and umbrella.")
    else:
        kind = icon_for(today["code"])
        parts.append({"sun": "Clear and dry.", "partly": "Dry, some clouds.", "cloud": "Gray but dry.", "fog": "Foggy start, then dry."}.get(kind, "Dry for now."))
        if today["low"] < 32 and now.hour < 11:
            parts.append("Frosty start, scrape the windshield.")
        elif today["low"] < 40 and now.hour < 12:
            parts.append("Cold start, wear layers.")
        elif today["high"] >= 90:
            parts.append("Hot afternoon, stay hydrated.")
        elif 62 <= today["high"] <= 80:
            parts.append("Nice day to be outside.")

    gust = max((r["gust"] or 0 for r in ahead), default=0)
    if gust >= 30:
        parts.append(f"Gusts to {round(gust)} mph.")
    return " ".join(parts)


def build_hourly(hours: list[dict[str, Any]], width: int = CHART_W) -> dict[str, Any]:
    n = len(hours)
    slot = width / n
    temps = [r["temp"] for r in hours]
    lo, hi = min(temps), max(temps)
    if hi - lo < 8:  # keep a flat day looking flat
        mid = (hi + lo) / 2
        lo, hi = mid - 4, mid + 4
    span = hi - lo

    def y(v: float) -> float:
        return TEMP_TOP + (hi - v) / span * (CHART_H - TEMP_TOP - TEMP_BOTTOM)

    pts = [(slot * (i + 0.5), y(t)) for i, t in enumerate(temps)]
    # Run the line to both edges so it doesn't stop half a slot short.
    line = [(0.0, pts[0][1])] + pts + [(float(width), pts[-1][1])]
    path = "M" + " L".join(f"{x:.1f} {yy:.1f}" for x, yy in line)

    marks = []
    for i in range(0, n, 3):
        r = hours[i]
        marks.append(
            {
                "pct": round((i + 0.5) / n * 100, 2),
                "x": round(pts[i][0], 1),
                "y": round(pts[i][1], 1),
                "temp": _deg(r["temp"]),
                "label": "NOW" if i == 0 else _axis_hour(r["t"]),
                "icon": icon_for(r["code"], r["day"]),
            }
        )

    bars = [{"x": round(slot * i + 1, 1), "w": round(slot - 2, 1), "h": round(r["prob"] / 100 * (CHART_H - 16), 1)} for i, r in enumerate(hours) if r["prob"] >= 5]

    peak = max(hours, key=lambda r: r["prob"])
    kind = precip_kind(peak["code"])
    # Snow is shown as snowfall; everything else as liquid precipitation.
    total = sum(r["snow"] for r in hours) if kind == "Snow" else sum(r["precip"] for r in hours)
    if peak["prob"] >= 20:
        note = f"{kind} {peak['prob']}% at {_hour(peak['t'])}"
        if total >= 0.01:
            note += f" · {_inches(total)} in"
    else:
        note = "No rain expected"

    out: dict[str, Any] = {"w": width, "h": CHART_H, "path": path, "bars": bars, "marks": marks, "note": note}
    if lo < 32 < hi:
        out["freeze_y"] = round(y(32), 1)
    # Night shading: stripes behind the hours between sunset and sunrise.
    nights, start = [], None
    for i, r in enumerate(hours + [{"day": True}]):
        if not r["day"] and start is None:
            start = i
        elif r["day"] and start is not None:
            nights.append({"x": round(slot * start, 1), "w": round(slot * (i - start), 1)})
            start = None
    out["nights"] = nights
    return out


def build_days(fc: dict[str, Any]) -> list[dict[str, Any]]:
    d = fc["daily"]
    n = min(DAYS, len(d["time"]))
    lows = [d["temperature_2m_min"][i] for i in range(n)]
    highs = [d["temperature_2m_max"][i] for i in range(n)]
    lo, hi = min(lows), max(highs)
    span = (hi - lo) or 1
    out = []
    for i in range(n):
        day = datetime.fromisoformat(d["time"][i])
        prob = (d.get("precipitation_probability_max") or [None] * n)[i]
        out.append(
            {
                "name": "TODAY" if i == 0 else day.strftime("%a").upper(),
                "date": f"{day.month}/{day.day}",
                "today": i == 0,
                "icon": icon_for(d["weather_code"][i]),
                "cond": condition(d["weather_code"][i]),
                "hi": _deg(highs[i]),
                "lo": _deg(lows[i]),
                # The bar spans this day's low to high on the week's scale, in % of the track.
                "top": round((hi - highs[i]) / span * 100, 1),
                "len": max(8.0, round((highs[i] - lows[i]) / span * 100, 1)),
                "prob": f"{prob}%" if prob is not None and prob >= 10 else "",
            }
        )
    return out


def build_details(fc: dict[str, Any], aq: dict[str, Any] | None, now: datetime) -> list[dict[str, str]]:
    c, d = fc["current"], fc["daily"]
    cells = []

    wind = round(c.get("wind_speed_10m") or 0)
    gust = round(c.get("wind_gusts_10m") or 0)
    cells.append({"k": "Wind", "v": f"{wind} mph {compass(c.get('wind_direction_10m'))}".strip() if wind else "Calm",
                  "s": f"gusts {gust} mph" if gust > wind + 4 else f"today up to {round(d['wind_speed_10m_max'][0])} mph"})

    cells.append({"k": "Humidity", "v": f"{round(c.get('relative_humidity_2m') or 0)}%", "s": f"dew point {_deg(c.get('dew_point_2m'))}"})

    uv_max = (d.get("uv_index_max") or [0])[0] or 0
    cells.append({"k": "UV today", "v": f"{round(uv_max)}", "s": uv_level(uv_max)})

    if aq and (aq.get("current") or {}).get("us_aqi") is not None:
        aqi = aq["current"]["us_aqi"]
        cells.append({"k": "Air quality", "v": f"{round(aqi)}", "s": aqi_level(aqi)})
    else:
        cells.append({"k": "Pressure", "v": f"{(c.get('pressure_msl') or 1013) * 0.02953:.2f}", "s": "inHg"})

    rise, set_ = _local(d["sunrise"][0]), _local(d["sunset"][0])
    secs = int(d.get("daylight_duration", [0])[0] or (set_ - rise).total_seconds())
    if now > set_ and len(d["sunrise"]) > 1:
        rise = _local(d["sunrise"][1])
        cells.append({"k": "Sunrise", "v": _short_clock(rise), "s": "tomorrow"})
    else:
        cells.append({"k": "Sun", "v": f"{_short_clock(rise)}–{_short_clock(set_)}", "s": f"{secs // 3600}h {secs % 3600 // 60}m of daylight"})

    snow = (d.get("snowfall_sum") or [0])[0] or 0
    rain = (d.get("precipitation_sum") or [0])[0] or 0
    prob = (d.get("precipitation_probability_max") or [0])[0] or 0
    if snow >= 0.1:
        cells.append({"k": "Snow today", "v": f"{_inches(snow)} in", "s": f"{prob}% chance"})
    else:
        cells.append({"k": "Rain today", "v": f"{_inches(rain)} in", "s": f"{prob}% chance" if prob else "none expected"})
    return cells


# ── Scene pictures: drawings of the house for each season and kind of weather ──
SCENE_EXTS = (".png", ".jpg", ".jpeg", ".webp")
# What to try for each kind of weather, after "<season>-<kind>". The first file found wins.
SCENE_FALLBACKS = {
    "snowing": ["snowing", "snow-cover", "rain", "cloudy"],
    "snow-cover": ["snow-cover", "cloudy", "clear"],
    "rain": ["rain", "cloudy"],
    "windy": ["windy", "cloudy", "clear"],
    "clear": ["clear", "cloudy"],
    "cloudy": ["cloudy", "clear"],
}
_scene_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def season(now: datetime) -> str:
    """Leafy months get their own pictures; bare-tree months (Oct-Mar) use the plain ones."""
    m = now.month
    if m in (4, 5):
        return "spring"
    if m in (6, 7, 8, 9):
        return "summer"
    if m in (10, 11):
        return "fall"
    return "winter"


def scene_kind(fc: dict[str, Any], hours: list[dict[str, Any]], snow_in: float) -> str:
    """snowing, rain, snow-cover, windy, clear or cloudy, from what's happening now."""
    c = fc["current"]
    code = c.get("weather_code")
    soon = hours[:2]
    if code in SNOW or any(r["prob"] >= 50 and r["code"] in SNOW for r in soon):
        return "snowing"
    if code in RAIN or code in STORM or any(r["prob"] >= 60 and r["code"] in (RAIN | STORM) for r in soon[:1]):
        return "rain"
    if snow_in >= 1:
        return "snow-cover"
    if (c.get("wind_gusts_10m") or 0) >= 25 or (c.get("wind_speed_10m") or 0) >= 15:
        return "windy"
    if code in (0, 1):
        return "clear"
    return "cloudy"


def _scene_file(name: str) -> str | None:
    for ext in SCENE_EXTS:
        path = os.path.join(SCENE_DIR, name + ext)
        if os.path.isfile(path):
            return path
    return None


DRY = ("clear", "cloudy", "windy")


def pick_scene_file(kind: str, when: str, night: bool = False) -> str | None:
    """Most specific first: summer-rain, then summer (any dry day), then rain, then fallbacks.

    After dark, a night picture wins on dry evenings (winter-night, then night). Rain, snow
    and snow on the ground keep their own pictures unless there's a night-rain and so on.
    """
    names = []
    if night:
        names += [f"{when}-night-{kind}", f"night-{kind}"]
        if kind in DRY:
            names += [f"{when}-night", "night"]
    names.append(f"{when}-{kind}")
    if when in ("spring", "summer") and kind in DRY:
        names.append(when)  # one leafy picture covers any dry day
    names += SCENE_FALLBACKS.get(kind, [kind])
    if when in ("spring", "summer"):
        names.append(when)
    for name in names:
        path = _scene_file(name)
        if path:
            return path
    return None


def eink_scene_png(path: str, width: int = SCENE_W, height: int = SCENE_H) -> bytes:
    """Crop a scene drawing to width x height and dither it to 1-bit for e-ink.

    The outer few percent are trimmed so hand-drawn frames don't show, the crop keeps
    the house (a little above centre) and Floyd-Steinberg dithering keeps the halftone
    look. Rendered at exactly the panel size so the browser never rescales it.
    """
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps

    im = ImageOps.exif_transpose(Image.open(path))
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        flat = Image.new("RGBA", im.size, (255, 255, 255, 255))
        flat.alpha_composite(im)
        im = flat
    im = im.convert("L")
    ix, iy = round(im.width * 0.03), round(im.height * 0.035)
    im = im.crop((ix, iy, im.width - ix, im.height - iy))
    scale = max(width / im.width, height / im.height)
    im = im.resize((round(im.width * scale), round(im.height * scale)), Image.Resampling.LANCZOS)
    focus = float(os.getenv("SCENE_FOCUS") or (0.45 if im.width / im.height > 1.6 else 0.38))
    x = (im.width - width) // 2
    y = round((im.height - height) * min(max(focus, 0.0), 1.0))
    im = im.crop((x, y, x + width, y + height))
    im = ImageOps.autocontrast(im, cutoff=1)
    im = ImageEnhance.Contrast(im).enhance(1.3).filter(ImageFilter.UnsharpMask(1.5, 120, 2))
    buf = io.BytesIO()
    im.convert("1").save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def load_scene(path: str) -> dict[str, Any] | None:
    """The scene as a data URI, converted once and again only when the file changes."""
    try:
        mtime = os.path.getmtime(path)
        cached = _scene_cache.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
        png = eink_scene_png(path)
        scene = {"src": "data:image/png;base64," + base64.b64encode(png).decode(), "w": SCENE_W, "h": SCENE_H,
                 "name": os.path.splitext(os.path.basename(path))[0]}
        _scene_cache[path] = (mtime, scene)
        log.info("Scene picture loaded: %s", path)
        return scene
    except Exception as err:
        log.warning("Could not load scene picture %s: %s", path, err)
        return None


def snow_depth_inches(fc: dict[str, Any]) -> float:
    depth = (fc.get("current") or {}).get("snow_depth") or 0
    unit = ((fc.get("current_units") or {}).get("snow_depth") or "m").lower()
    return depth * {"m": 39.37, "cm": 0.3937, "ft": 12, "inch": 1, "in": 1, '"': 1}.get(unit, 39.37)


def choose_scene(fc: dict[str, Any], hours: list[dict[str, Any]], now: datetime) -> dict[str, Any] | None:
    if SCENE_MODE == "off" or not os.path.isdir(SCENE_DIR):
        return None
    if SCENE_MODE != "auto":
        path = _scene_file(SCENE_MODE)
    else:
        night = not bool((fc.get("current") or {}).get("is_day", 1))
        path = pick_scene_file(scene_kind(fc, hours, snow_depth_inches(fc)), season(now), night)
    return load_scene(path) if path else None


SEVERITY = {"Extreme": 4, "Severe": 3, "Moderate": 2, "Minor": 1}


def build_alert(alerts: dict[str, Any] | None, now: datetime) -> dict[str, Any] | None:
    feats = [f.get("properties") or {} for f in (alerts or {}).get("features") or []]
    feats = [p for p in feats if p.get("event") and p.get("status", "Actual") == "Actual" and p.get("messageType") != "Cancel"]
    if not feats:
        return None
    feats.sort(key=lambda p: (-SEVERITY.get(p.get("severity"), 0), p.get("onset") or p.get("effective") or ""))
    p = feats[0]
    until = ""
    end = p.get("ends") or p.get("expires")
    if end:
        try:
            t = datetime.fromisoformat(end).astimezone(TZ)
            until = f"until {_hour(t)}" if t.date() == now.date() else f"until {t:%a} {_hour(t)}"
        except ValueError:
            pass
    start = p.get("onset")
    if start:
        try:
            t = datetime.fromisoformat(start).astimezone(TZ)
            if t > now + timedelta(minutes=30):
                until = (f"from {_hour(t)}" if t.date() == now.date() else f"from {t:%a} {_hour(t)}") + (" " + until if until else "")
        except ValueError:
            pass
    return {"event": p["event"], "when": until, "more": len(feats) - 1}


def build_panel(fc: dict[str, Any], aq: dict[str, Any] | None, alerts: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
    c, d = fc["current"], fc["daily"]
    is_day = bool(c.get("is_day", 1))
    today = {"code": d["weather_code"][0], "high": d["temperature_2m_max"][0], "low": d["temperature_2m_min"][0]}
    hours = hourly_window(fc, now)
    scene = choose_scene(fc, hours, now)
    panel: dict[str, Any] = {
        "ok": True,
        "as_of": _clock(now),
        "demo": DEMO,
        "place": PLACE,
        "now": {
            "temp": _deg(c["temperature_2m"]).rstrip("°"),
            "feels": _deg(c.get("apparent_temperature")),
            "icon": icon_for(c["weather_code"], is_day),
            "cond": condition(c["weather_code"], is_day),
            "hi": _deg(today["high"]),
            "lo": _deg(today["low"]),
            "plan": plan_text(hours, today, now) if hours else "",
        },
        "details": build_details(fc, aq, now),
        "hourly": build_hourly(hours, SCENE_CHART_W if scene else CHART_W) if len(hours) >= 6 else None,
        "days": build_days(fc),
    }
    if scene:
        panel["scene"] = scene
    alert = build_alert(alerts, now)
    if alert:
        panel["alert"] = alert
    return panel


# ── Demo data: an Open-Meteo-shaped forecast for a cold November day with rain coming ──
def _demo_raw(now: datetime) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    rnd = random.Random(now.date().toordinal())
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    days = []
    for i, (lo, hi, code, prob, rain) in enumerate(
        [(36, 54, 63, 80, 0.42), (41, 50, 61, 60, 0.15), (33, 47, 3, 10, 0), (29, 45, 0, 0, 0),
         (31, 52, 1, 0, 0), (38, 57, 2, 20, 0), (40, 55, 80, 50, 0.2)]
    ):
        days.append((midnight + timedelta(days=i), lo, hi, code, prob, rain))
    times, temps, probs, precs, codes, isday, gusts = [], [], [], [], [], [], []
    for k in range(24 * DAYS):
        t = midnight + timedelta(hours=k)
        dd = days[min(k // 24, DAYS - 1)]
        lo, hi = dd[1], dd[2]
        # Coldest near 6 AM, warmest near 3 PM.
        f = (math.cos((t.hour - 15) / 24 * 2 * math.pi) + 1) / 2
        temps.append(round(lo + (hi - lo) * f + rnd.uniform(-0.6, 0.6), 1))
        rainy = k // 24 == 0 and 15 <= t.hour <= 21 or k // 24 == 1 and 0 <= t.hour <= 7
        p = (85 if 17 <= t.hour <= 19 else 65) if rainy else (25 if k // 24 == 0 and t.hour in (13, 14, 22, 23) else rnd.choice((0, 0, 5)))
        probs.append(p)
        precs.append(round(rnd.uniform(0.03, 0.09), 2) if p >= 60 else 0)
        codes.append(63 if p >= 80 else 61 if p >= 60 else (3 if t.hour > 10 and k // 24 == 0 else 2) if k < 48 else dd[3])
        isday.append(1 if 7 <= t.hour < 17 else 0)
        gusts.append(round(rnd.uniform(12, 20) + (6 if rainy else 0)))
    fc = {
        "current": {
            "temperature_2m": 38.4, "apparent_temperature": 33.1, "relative_humidity_2m": 71, "dew_point_2m": 30,
            "weather_code": 2, "is_day": 1 if 7 <= now.hour < 17 else 0, "wind_speed_10m": 8.2,
            "wind_direction_10m": 315, "wind_gusts_10m": 17.5, "pressure_msl": 1021.4,
        },
        "hourly": {
            "time": [t.strftime("%Y-%m-%dT%H:%M") for t in (midnight + timedelta(hours=k) for k in range(24 * DAYS))],
            "temperature_2m": temps, "precipitation_probability": probs, "precipitation": precs,
            "weather_code": codes, "is_day": isday, "wind_gusts_10m": gusts,
        },
        "daily": {
            "time": [dd[0].strftime("%Y-%m-%d") for dd in days],
            "weather_code": [dd[3] for dd in days],
            "temperature_2m_max": [dd[2] for dd in days],
            "temperature_2m_min": [dd[1] for dd in days],
            "precipitation_probability_max": [dd[4] for dd in days],
            "precipitation_sum": [dd[5] for dd in days],
            "snowfall_sum": [0] * DAYS,
            "sunrise": [(dd[0] + timedelta(hours=6, minutes=56 + i)).strftime("%Y-%m-%dT%H:%M") for i, dd in enumerate(days)],
            "sunset": [(dd[0] + timedelta(hours=16, minutes=52 - i)).strftime("%Y-%m-%dT%H:%M") for i, dd in enumerate(days)],
            "daylight_duration": [35760 - 90 * i for i in range(DAYS)],
            "uv_index_max": [2.1, 1.4, 3.0, 3.2, 2.9, 2.5, 1.8],
            "wind_speed_10m_max": [14, 16, 11, 9, 8, 10, 15],
        },
    }
    aq = {"current": {"us_aqi": 34}}
    alerts = {"features": []}
    return fc, aq, alerts


# ── Live data ──
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
AIR_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
ALERTS_URL = "https://api.weather.gov/alerts/active"


class Source:
    def __init__(self) -> None:
        self.panel: dict[str, Any] | None = None
        self.error: str | None = None
        self.last_ok: datetime | None = None

    async def _air(self, client: httpx.AsyncClient) -> dict[str, Any] | None:
        try:
            r = await client.get(AIR_URL, params={"latitude": LAT, "longitude": LON, "current": "us_aqi", "timezone": TZ_NAME})
            r.raise_for_status()
            return r.json()
        except Exception as err:  # optional: the tile falls back to pressure
            log.warning("Air quality fetch failed: %s", err)
            return None

    async def _alerts(self, client: httpx.AsyncClient) -> dict[str, Any] | None:
        if not ALERTS:
            return None
        try:
            r = await client.get(ALERTS_URL, params={"point": f"{LAT:.4f},{LON:.4f}"}, headers={"Accept": "application/geo+json"})
            r.raise_for_status()
            return r.json()
        except Exception as err:  # optional: no alert box
            log.warning("NWS alerts fetch failed: %s", err)
            return None

    async def refresh(self) -> None:
        now = datetime.now(TZ)
        if DEMO:
            fc, aq, alerts = _demo_raw(now)
            self.panel, self.error, self.last_ok = build_panel(fc, aq, alerts, now), None, now
            return
        params = {
            "latitude": LAT,
            "longitude": LON,
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,dew_point_2m,weather_code,is_day,"
            "wind_speed_10m,wind_direction_10m,wind_gusts_10m,pressure_msl,snow_depth",
            "hourly": "temperature_2m,precipitation_probability,precipitation,snowfall,weather_code,is_day,wind_gusts_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum,"
            "snowfall_sum,sunrise,sunset,daylight_duration,uv_index_max,wind_speed_10m_max",
            "temperature_unit": "fahrenheit",
            "wind_speed_unit": "mph",
            "precipitation_unit": "inch",
            "timezone": TZ_NAME,
            "forecast_days": DAYS + 1,  # an extra day so the 24-hour chart never runs short
        }
        async with httpx.AsyncClient(timeout=20, headers={"User-Agent": USER_AGENT}) as client:
            r = await client.get(FORECAST_URL, params=params)
            r.raise_for_status()
            fc = r.json()
            aq, alerts = await asyncio.gather(self._air(client), self._alerts(client))
        panel = build_panel(fc, aq, alerts, now)
        self.panel, self.error, self.last_ok = panel, None, now
        log.info("Refreshed: %s°F, %s%s", panel["now"]["temp"], panel["now"]["cond"], f", alert: {panel['alert']['event']}" if panel.get("alert") else "")


source = Source()
app = FastAPI(title="weather-bridge")


async def _loop() -> None:
    while True:
        try:
            await source.refresh()
        except Exception as err:  # keep serving the last good data
            source.error = f"{type(err).__name__}: {err}"
            log.warning("Refresh failed: %s", source.error)
        await asyncio.sleep(REFRESH_MINUTES * 60)


@app.on_event("startup")
async def _start() -> None:
    asyncio.create_task(_loop())


def _check(request: Request) -> None:
    if not BRIDGE_TOKEN:
        return
    got = request.headers.get("authorization", "").removeprefix("Bearer ").strip() or request.query_params.get("token", "")
    if not secrets.compare_digest(got, BRIDGE_TOKEN):
        raise HTTPException(401, "bad token")


@app.get("/weather.json")
async def weather_json(request: Request) -> JSONResponse:
    _check(request)
    if source.panel:
        out = dict(source.panel)
        if source.error:
            out["warning"] = "Couldn't reach Open-Meteo; showing data from " + out.get("as_of", "earlier")
        return JSONResponse(out)
    return JSONResponse({"ok": False, "error": source.error or "Starting up…"})


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": source.error is None and source.panel is not None,
        "demo": DEMO,
        "last_ok": source.last_ok.isoformat() if source.last_ok else None,
        "error": source.error,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8765")))
