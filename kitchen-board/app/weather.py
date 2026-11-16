"""Weather from Open-Meteo (free, no API key)."""
import json
import logging
import time
import urllib.parse
import urllib.request
from datetime import datetime

import config

log = logging.getLogger("weather")

_cache: dict = {"at": 0.0, "raw": None}

SNOW = {71, 73, 75, 77, 85, 86}
STORM = {95, 96, 99}
RAIN = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82}
FOG = {45, 48}


def icon_for(code: int) -> str:
    if code in STORM:
        return "storm"
    if code in SNOW:
        return "snow"
    if code in RAIN:
        return "rain"
    if code in FOG:
        return "fog"
    if code == 0:
        return "sun"
    if code in (1, 2):
        return "partly"
    return "cloud"


def _fetch_raw() -> dict | None:
    if _cache["raw"] is not None and time.time() - _cache["at"] < config.CACHE_SECONDS:
        return _cache["raw"]
    params = {
        "latitude": config.WEATHER_LAT,
        "longitude": config.WEATHER_LON,
        "current": "temperature_2m,weather_code",
        "hourly": "precipitation_probability,weather_code",
        "daily": "temperature_2m_max,temperature_2m_min,weather_code",
        "temperature_unit": "fahrenheit",
        "timezone": config.TZ_NAME,
        "forecast_days": 2,
    }
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "kitchen-board/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = json.load(resp)
        _cache.update(at=time.time(), raw=raw)
        return raw
    except Exception as exc:  # network down, API error
        log.warning("weather fetch failed: %s", exc)
        return _cache["raw"]  # last good copy, if any


def _hour_label(dt: datetime) -> str:
    h = dt.hour % 12 or 12
    return f"{h} {'AM' if dt.hour < 12 else 'PM'}"


def summarize(raw: dict, now: datetime) -> dict:
    current = raw["current"]
    daily = raw["daily"]
    hourly = raw["hourly"]
    high = round(daily["temperature_2m_max"][0])
    low = round(daily["temperature_2m_min"][0])

    # First hour from now until 9 PM with a 50%+ chance of precipitation.
    wet_at, wet_code = None, None
    for t, prob, code in zip(hourly["time"], hourly["precipitation_probability"], hourly["weather_code"]):
        ht = datetime.fromisoformat(t).replace(tzinfo=config.TZ)
        if ht.date() != now.date() or ht.hour < now.hour or ht.hour > 21:
            continue
        if prob is not None and prob >= 50:
            wet_at, wet_code = ht, code
            break

    parts = []
    if wet_at is not None:
        kind = "Snow" if wet_code in SNOW else "Storms" if wet_code in STORM else "Rain"
        if wet_at.hour <= now.hour:
            parts.append(f"{kind} this hour.")
        else:
            parts.append(f"{kind} after {_hour_label(wet_at)}.")
        parts.append("Grab a jacket." if low < 50 else "Bring an umbrella.")
    else:
        code = daily["weather_code"][0]
        parts.append({"sun": "Clear and dry.", "partly": "Dry, some clouds.",
                      "cloud": "Gray but dry.", "fog": "Foggy start."}.get(icon_for(code), "Dry today."))
        if low < 40:
            parts.append("Cold start, wear layers.")
        elif high >= 85:
            parts.append("Hot afternoon.")

    return {
        "temp": round(current["temperature_2m"]),
        "high": high,
        "low": low,
        "icon": icon_for(current["weather_code"]),
        "summary": " ".join(parts),
    }


def sample_weather() -> dict:
    return {"temp": 48, "high": 56, "low": 39, "icon": "partly",
            "summary": "Rain after 4 PM. Grab a jacket."}


def get_weather(now: datetime) -> dict | None:
    if config.SAMPLE_DATA == "on":
        return sample_weather()
    raw = _fetch_raw()
    if raw is None:
        return None
    try:
        return summarize(raw, now)
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        log.warning("weather parse failed: %s", exc)
        return None
