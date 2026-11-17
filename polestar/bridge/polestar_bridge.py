"""
Polestar -> LaraPaper bridge.

Logs in to your Polestar account with the community `pypolestar` library,
refreshes every few minutes in the background, and serves a small,
pre-formatted JSON document that a LaraPaper (TRMNL BYOS) recipe polls.

    GET /polestar.json?units=imperial   (or units=metric)
    GET /images?token=...               page linking Polestar's official images of your cars
    GET /images.json                    the same links as JSON
    GET /health

Auth: send `Authorization: Bearer <BRIDGE_TOKEN>` (or ?token=...).

Environment:
    POLESTAR_USERNAME   Polestar ID e-mail             (not needed in DEMO mode)
    POLESTAR_PASSWORD   Polestar ID password
    POLESTAR_VINS       optional, comma-separated VINs to include (default: all)
    CAR_NAMES           optional, "VIN_or_last4=Nickname,..." e.g. "4321=Blue,8765=Snow"
    BRIDGE_TOKEN        shared secret LaraPaper must send (strongly recommended)
    REFRESH_MINUTES     background refresh interval, default 10
    TZ                  IANA time zone for clock labels, e.g. America/New_York
    DEMO                "1" to serve two fake cars without logging in
    LOGO_FILE           optional image shown on the right of the panel (default: logo.png next to
                        this script, if present). Converted to crisp 1-bit for e-ink at startup.
    LOGO_SIZE           pixel size of the logo box on screen, default 220
    LOGO_SMALL_SIZE     pixel size of the bold logo in front of each car's name, default 46
    PHOTO_FILE          optional car image(s) for the left of the two-car screen: a file, a folder, or a
                        comma-separated list (default: every image in cars/ next to this script, plus
                        car.jpg/png). With several, one is picked at random on each refresh.
    PHOTO_FOCUS         0-1, which part of a wide image to keep (0 = left edge). Default 0 for drawings
                        on a light background, 0.3 for photos
    PHOTO_ZOOM          drawings only: 1.0 (default) fits the whole car; larger values enlarge and crop it
    PHOTO_WIDTH         width of the image panel in px, default 480 (60% of the screen), 300-520
"""

from __future__ import annotations

import asyncio
import base64
import io
import contextlib
import html
import logging
import math
import os
import random
import re
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

log = logging.getLogger("polestar-bridge")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")

KM_PER_MI = 1.609344
REFRESH_MINUTES = max(2, int(os.getenv("REFRESH_MINUTES", "10")))
STALE_AFTER = timedelta(hours=int(os.getenv("STALE_HOURS", "6")))
BRIDGE_TOKEN = os.getenv("BRIDGE_TOKEN", "")
DEMO = os.getenv("DEMO", "").lower() in {"1", "true", "yes"}
TZ = ZoneInfo(os.getenv("TZ") or "UTC")


# --------------------------------------------------------------------------- #
# Normalised, unit-neutral car snapshot (metric internally)
# --------------------------------------------------------------------------- #
@dataclass
class CarSnapshot:
    vin: str
    model: str | None = None
    model_year: str | None = None
    registration: str | None = None
    percent: float | None = None
    target_percent: int | None = None
    range_km: float | None = None
    charging_status: str | None = None  # enum value text, e.g. "Charging", "Smart Charging"
    connection: str | None = None  # "Connected" / "Disconnected" / "Fault"
    power_w: int | None = None
    charging_type: str | None = None  # "AC" / "DC"
    minutes_to_full: int | None = None
    consumption_kwh_100km: float | None = None
    odometer_m: int | None = None
    days_to_service: int | None = None
    km_to_service: int | None = None
    warnings: list[str] = field(default_factory=list)
    images: list[dict[str, Any]] = field(default_factory=list)  # Polestar's official renders of this car
    event_time: datetime | None = None  # when the car last reported battery data
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class State:
    cars: list[CarSnapshot] = field(default_factory=list)
    last_success: datetime | None = None
    error: str | None = None


state = State()


# --------------------------------------------------------------------------- #
# Polestar fetching
# --------------------------------------------------------------------------- #
def _enum_text(value: Any) -> str | None:
    if value is None:
        return None
    text = getattr(value, "value", str(value))
    return None if text in {"Unspecified", "None"} else text


def _short_warning(label: str, text: str) -> str:
    """Turn the API's long enum labels into something that fits on a small screen."""
    if label == "Service":
        if "Overdue" in text:
            return "Service overdue"
        if "Almost" in text:
            return "Service due soon"
        if "Time For Service" in text:
            return "Service due now"
        return "Service required"
    return f"{label} {text.lower()}"  # e.g. "Brake fluid too low"


def _car_images(api: Any, vin: str, info: Any) -> list[dict[str, Any]]:
    """Official renders of this exact car (colour, wheels, trim), as served by Polestar."""
    images: list[dict[str, Any]] = []
    try:
        data = api.get_car_images(vin)
    except Exception as exc:
        log.debug("No car images for %s: %s", vin[-4:], exc)
        data = None
    if data:
        for background, items in (("transparent", data.transparent), ("opaque", data.opaque)):
            for img in sorted(items, key=lambda i: i.angle):
                images.append({"angle": img.angle, "background": background, "url": img.url})
    legacy = getattr(info, "image_url", None) if info else None
    if legacy and all(i["url"] != legacy for i in images):
        images.append({"angle": None, "background": "default", "url": legacy})
    return images


def _snapshot_from_api(api: Any, vin: str) -> CarSnapshot:
    info = api.get_car_information(vin)
    tele = api.get_car_telematics(vin)
    grpc_batt = api.get_grpc_battery(vin)
    grpc_soc = api.get_grpc_target_soc(vin)

    snap = CarSnapshot(vin=vin)
    if info:
        snap.model = info.model_name
        snap.model_year = info.model_year
        snap.registration = info.registration_no

    snap.images = _car_images(api, vin, info)

    batt = tele.battery if tele else None
    if batt:
        snap.percent = batt.battery_charge_level_percentage
        snap.range_km = batt.estimated_distance_to_empty_km
        snap.minutes_to_full = batt.estimated_charging_time_to_full_minutes
        snap.charging_status = _enum_text(batt.charging_status)
        snap.event_time = batt.event_updated_timestamp

    # gRPC data is richer (power, plug state, AC/DC) and usually fresher.
    if grpc_batt:
        if grpc_batt.battery_charge_level_percentage is not None:
            snap.percent = grpc_batt.battery_charge_level_percentage
        snap.range_km = grpc_batt.estimated_distance_to_empty_km or snap.range_km
        snap.minutes_to_full = grpc_batt.estimated_charging_time_to_full_minutes or snap.minutes_to_full
        snap.charging_status = _enum_text(grpc_batt.charging_status) or snap.charging_status
        snap.connection = _enum_text(grpc_batt.charger_connection_status)
        snap.power_w = grpc_batt.charging_power_watts
        snap.charging_type = _enum_text(grpc_batt.charging_type)
        snap.consumption_kwh_100km = grpc_batt.average_energy_consumption_kwh_per_100km
        if grpc_batt.timestamp:
            snap.event_time = grpc_batt.timestamp

    if grpc_soc and grpc_soc.battery_charge_target_level:
        snap.target_percent = grpc_soc.battery_charge_target_level

    if tele and tele.odometer:
        snap.odometer_m = tele.odometer.odometer_meters

    if tele and tele.health:
        h = tele.health
        snap.days_to_service = h.days_to_service
        snap.km_to_service = h.distance_to_service_km
        # Service reminders are deliberately left out; only fluid-level warnings are shown.
        for label, value in (
            ("Brake fluid", h.brake_fluid_level_warning),
            ("Coolant", h.engine_coolant_level_warning),
            ("Oil", h.oil_level_warning),
        ):
            text = _enum_text(value)
            if text and text != "No Warning":
                snap.warnings.append(_short_warning(label, text))

    return snap


class PolestarPoller:
    def __init__(self) -> None:
        self.api: Any = None

    async def _login(self) -> None:
        from pypolestar import PolestarApi  # imported lazily so DEMO mode needs no login

        vins = [v.strip() for v in os.getenv("POLESTAR_VINS", "").split(",") if v.strip()] or None
        self.api = PolestarApi(
            username=os.environ["POLESTAR_USERNAME"],
            password=os.environ["POLESTAR_PASSWORD"],
            vins=vins,
        )
        await self.api.async_init()
        log.info("Logged in; VINs: %s", ", ".join(v[-4:] for v in self.api.get_available_vins()))

    async def refresh(self) -> None:
        if self.api is None:
            await self._login()
        cars = []
        for vin in sorted(self.api.get_available_vins()):
            await self.api.update_latest_data(vin, update_telematics=True, update_grpc=True)
            cars.append(_snapshot_from_api(self.api, vin))
        state.cars = cars
        state.last_success = datetime.now(timezone.utc)
        state.error = None

    async def reset(self) -> None:
        if self.api is not None:
            with contextlib.suppress(Exception):
                await self.api.async_logout()
        self.api = None


def _demo_cars() -> list[CarSnapshot]:
    now = datetime.now(timezone.utc)
    swing = (math.sin(now.timestamp() / 3600) + 1) / 2  # drifts slowly so the panel visibly changes
    p3_pct = round(45 + 40 * swing)
    return [
        CarSnapshot(
            vin="DEMO0000000003333", model="Polestar 3", model_year="2025",
            percent=p3_pct, target_percent=90, range_km=round(p3_pct * 4.8),
            charging_status="Charging", connection="Connected", power_w=11040, charging_type="AC",
            minutes_to_full=round((90 - p3_pct) * 7.5), consumption_kwh_100km=22.4,
            odometer_m=20_085_000, days_to_service=214, km_to_service=14_300, event_time=now - timedelta(minutes=4),
        ),
        CarSnapshot(
            vin="DEMO0000000002222", model="Polestar 2", model_year="2023",
            percent=63 + random.randint(-1, 1), target_percent=80, range_km=262,
            charging_status="Idle", connection="Disconnected", consumption_kwh_100km=18.9,
            odometer_m=48_512_000, days_to_service=41, km_to_service=2_100,
            event_time=now - timedelta(minutes=38),
        ),
    ]


async def refresher() -> None:
    poller = PolestarPoller()
    failures = 0
    while True:
        try:
            if DEMO:
                state.cars = _demo_cars()
                state.last_success = datetime.now(timezone.utc)
            elif not (os.getenv("POLESTAR_USERNAME") and os.getenv("POLESTAR_PASSWORD")):
                state.error = "Set POLESTAR_USERNAME and POLESTAR_PASSWORD in larapaper/.env (or DEMO=1)"
                log.warning(state.error)
                await asyncio.sleep(300)
                continue
            else:
                await poller.refresh()
            failures = 0
            delay = REFRESH_MINUTES * 60
        except Exception as exc:  # keep serving the last good data
            failures += 1
            state.error = f"{type(exc).__name__}: {exc}"[:200]
            log.warning("Refresh failed (%d): %s", failures, state.error)
            await poller.reset()  # force a fresh login next time
            delay = min(REFRESH_MINUTES * 60, 30 * 2 ** min(failures, 5))
        await asyncio.sleep(delay)


# --------------------------------------------------------------------------- #
# Formatting for the e-ink template
# --------------------------------------------------------------------------- #
def _clock(dt: datetime | None, now: datetime) -> str | None:
    if dt is None:
        return None
    local = dt.astimezone(TZ)
    text = local.strftime("%-I:%M %p")
    if local.date() != now.astimezone(TZ).date():
        text = local.strftime("%a ") + text
    return text


def _duration(minutes: int | None) -> str | None:
    if not minutes or minutes <= 0:
        return None
    h, m = divmod(int(minutes), 60)
    return f"{h} h {m:02d} min" if h else f"{m} min"


def _num(value: float | None, digits: int = 0) -> str | None:
    if value is None:
        return None
    return f"{value:,.{digits}f}"


def _name_overrides() -> dict[str, str]:
    overrides: dict[str, str] = {}
    for pair in os.getenv("CAR_NAMES", "").split(","):
        if "=" in pair:
            key, name = pair.split("=", 1)
            overrides[key.strip().upper()] = name.strip()
    return overrides


def _ordered(cars: list[CarSnapshot]) -> list[CarSnapshot]:
    """Cars listed in CAR_NAMES come first, in that order (so Snow can sit above Midnight)."""
    keys = list(_name_overrides())

    def rank(c: CarSnapshot) -> int:
        for i, key in enumerate(keys):
            if key in (c.vin.upper(), c.vin[-4:].upper()):
                return i
        return len(keys)

    return sorted(cars, key=rank)


def _display_names(cars: list[CarSnapshot]) -> dict[str, str]:
    overrides = _name_overrides()
    models = [c.model for c in cars]
    names = {}
    for c in cars:
        name = overrides.get(c.vin.upper()) or overrides.get(c.vin[-4:].upper())
        if not name:
            name = c.model or "Polestar"
            if models.count(c.model) > 1:
                name += f" ·{c.registration or c.vin[-4:]}"
        names[c.vin] = name
    return names


# Polestar sometimes reports a nonsense average consumption (seen: ~0.9 kWh/100 km,
# which would read "69 mi/kWh"). Real EVs land around 10-40 kWh/100 km. Outside
# this range the screen shows the car's last sensible value, or "Pending" until one
# arrives. The last values are kept in memory, so a bridge restart clears them.
SANE_CONSUMPTION_KWH_100KM = (8.0, 60.0)
_last_sane_consumption: dict[str, float] = {}


def _sane_consumption(c: CarSnapshot) -> float | None:
    low, high = SANE_CONSUMPTION_KWH_100KM
    value = c.consumption_kwh_100km
    if value is not None and low <= value <= high:
        _last_sane_consumption[c.vin] = value
        return value
    if value is not None and c.vin not in _last_sane_consumption:
        log.info("Ignoring implausible consumption %.2f kWh/100 km for VIN ...%s", value, c.vin[-4:])
    return _last_sane_consumption.get(c.vin)


def format_car(c: CarSnapshot, name: str, imperial: bool, now: datetime) -> dict[str, Any]:
    dist = (lambda km: km / KM_PER_MI) if imperial else (lambda km: km)
    status = c.charging_status or ""
    charging = status in {"Charging", "Smart Charging"}
    plugged = c.connection == "Connected" or charging
    if c.connection == "Fault" or status in {"Fault", "Error"}:
        state_key, label = "fault", "Charging fault"
    elif charging:
        state_key, label = "charging", status
    elif status == "Done":
        state_key, label = "done", "Charge complete"
    elif status in {"Scheduled", "Smart Charging Paused"}:
        state_key, label = "scheduled", "Charging scheduled" if status == "Scheduled" else "Smart charging paused"
    elif plugged:
        state_key, label = "plugged", "Plugged in"
    elif c.connection == "Disconnected":
        state_key, label = "unplugged", "Not plugged in"
    else:
        state_key, label = "unknown", "Parked"

    pct = None if c.percent is None else max(0, min(100, round(c.percent)))
    full_range = None
    if c.range_km and c.percent:
        full_range = c.range_km / c.percent * 100

    efficiency = None
    consumption = _sane_consumption(c)
    if consumption:
        efficiency = (
            _num(100 / (consumption * KM_PER_MI), 1)  # mi/kWh
            if imperial
            else _num(consumption, 1)  # kWh/100 km
        )
    # Reported, but nothing sensible yet: show "Pending" rather than hiding it.
    efficiency_pending = efficiency is None and bool(c.consumption_kwh_100km)

    full_at = None
    if charging and c.minutes_to_full:
        full_at = c.fetched_at + timedelta(minutes=c.minutes_to_full)

    event = c.event_time or c.fetched_at
    return {
        "name": name,
        "model": c.model,
        "model_year": c.model_year,
        "registration": c.registration,
        "vin_tail": c.vin[-4:],
        "percent": pct,
        "target": c.target_percent,
        "low": pct is not None and pct <= 20,
        "range": _num(dist(c.range_km)) if c.range_km is not None else None,
        "range_full": _num(dist(full_range)) if full_range else None,
        "state": state_key,
        "status": label,
        "charging": charging,
        "plugged": plugged,
        "power": f"{c.power_w / 1000:.1f} kW" if charging and c.power_w else None,
        "charge_type": c.charging_type if charging else None,
        "time_to_full": _duration(c.minutes_to_full) if charging else None,
        "full_by": _clock(full_at, now),
        "odometer": _num(dist(c.odometer_m / 1000)) if c.odometer_m is not None else None,
        "efficiency": "Pending" if efficiency_pending else efficiency,
        "efficiency_pending": efficiency_pending,
        "service": None,  # not shown on the panel (by request); days/distance remain in CarSnapshot
        "service_soon": False,
        "warnings": c.warnings,
        "warning_count": len(c.warnings),
        "as_of": _clock(event, now),
        "stale": now - event > STALE_AFTER,
    }



# --------------------------------------------------------------------------- #
# Optional logo (your own image file, prepared for 1-bit e-ink)
# --------------------------------------------------------------------------- #
_BAYER4 = [[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]]


def eink_logo_png(path: str, size: int, bold: bool = False) -> bytes:
    """Fit an image into a size x size box and turn it into a 1-bit PNG.

    Near-black stays solid, near-white stays white, and mid-tones become an even
    ordered-dither pattern so two-tone artwork still reads as two tones on e-ink.
    The result is rendered at exactly `size` px so the browser never rescales it.
    With `bold`, every non-white pixel goes solid black and thin strokes are
    thickened by a pixel, so a small logo stays strong next to bold text.
    """
    from PIL import Image  # only needed when a logo is configured

    src = Image.open(path).convert("RGBA")
    flat = Image.new("RGBA", src.size, (255, 255, 255, 255))
    flat.alpha_composite(src)
    gray = flat.convert("L")
    # Off-white backgrounds (scans, JPEGs) count as background too.
    bbox = gray.point(lambda v: 255 if v < 225 else 0).getbbox()
    if bbox:
        gray = gray.crop(bbox)
    gray.thumbnail((size, size), Image.Resampling.LANCZOS)
    canvas = Image.new("L", (size, size), 255)
    canvas.paste(gray, ((size - gray.width) // 2, (size - gray.height) // 2))

    if bold:
        from PIL import ImageFilter

        solid = canvas.point(lambda v: 0 if v < 232 else 255).filter(ImageFilter.MinFilter(3))
        buf = io.BytesIO()
        solid.convert("1").save(buf, format="PNG", optimize=True)
        return buf.getvalue()

    out = Image.new("1", (size, size), 1)
    src_px, out_px = canvas.load(), out.load()
    for y in range(size):
        for x in range(size):
            v = src_px[x, y]
            if v < 48:
                out_px[x, y] = 0  # solid black
            elif v > 232:
                out_px[x, y] = 1
            else:
                lifted = 110 + (v - 48) * (255 - 110) / (232 - 48)  # mid-tones -> ~50% pattern
                out_px[x, y] = 1 if lifted / 255 * 16 > _BAYER4[y % 4][x % 4] + 0.5 else 0
    buf = io.BytesIO()
    out.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def eink_photo_png(path: str, width: int, height: int, focus: float | None = None, zoom: float = 1.0) -> bytes:
    """Fit a car image into width x height and dither it to 1-bit.

    Two modes, picked automatically:
    * Drawing / cut-out on a light background: the background is trimmed, the car is
      scaled to `zoom` x the panel width (1.0 = whole car, larger values crop it) and
      `focus` picks which part stays (0 = the left end, default). The background is forced to pure
      white so it doesn't turn into dot noise.
    * Photo: cover-cropped to fill the area (focus default 0.3).
    Contrast is stretched and edges sharpened first so light subjects keep their shape.
    """
    from PIL import Image, ImageEnhance, ImageFilter, ImageOps, ImageStat

    im = ImageOps.exif_transpose(Image.open(path))
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        flat = Image.new("RGBA", im.size, (255, 255, 255, 255))
        flat.alpha_composite(im)
        im = flat
    im = im.convert("L")

    edge = Image.new("L", (im.width, 2))
    edge.paste(im.crop((0, 0, im.width, 1)), (0, 0))
    edge.paste(im.crop((0, im.height - 1, im.width, im.height)), (0, 1))
    light_background = ImageStat.Stat(edge).mean[0] > 225

    if light_background:
        focus = 0.0 if focus is None else focus
        bbox = im.point(lambda v: 255 if v < 200 else 0).getbbox()
        if bbox:
            im = im.crop(bbox)
        target_w = (width - 16) * zoom if zoom <= 1 else width * zoom  # zoom <= 1: whole car with a margin
        scale = min(target_w / im.width, (height - 16) / im.height)
        im = im.resize((round(im.width * scale), round(im.height * scale)), Image.Resampling.LANCZOS)
        canvas = Image.new("L", (width, height), 255)
        overflow = max(0, im.width - width + 8)
        x = 8 - round(overflow * min(max(focus, 0.0), 1.0)) if overflow else (width - im.width) // 2
        canvas.paste(im, (x, (height - im.height) // 2))
        im = ImageOps.autocontrast(canvas, cutoff=1).filter(ImageFilter.UnsharpMask(1.5, 100, 2))
        im = im.point(lambda v: 255 if v > 225 else v)  # clean paper-white background
    else:
        focus = 0.3 if focus is None else focus
        scale = max(width / im.width, height / im.height)
        im = im.resize((round(im.width * scale), round(im.height * scale)), Image.Resampling.LANCZOS)
        x = round((im.width - width) * min(max(focus, 0.0), 1.0))
        y = (im.height - height) // 2
        im = im.crop((x, y, x + width, y + height))
        im = ImageOps.autocontrast(im, cutoff=2)
        im = ImageEnhance.Contrast(im).enhance(1.4).filter(ImageFilter.UnsharpMask(2, 120, 2))

    buf = io.BytesIO()
    im.convert("1").save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _data_uri(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode()


def _find_file(env_name: str, *defaults: str) -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    if os.getenv(env_name):
        return os.environ[env_name]
    for name in defaults:
        path = os.path.join(here, name)
        if os.path.isfile(path):
            return path
    return ""


def _load_logo() -> dict[str, Any] | None:
    path = _find_file("LOGO_FILE", "logo.png")
    if path and not os.path.isfile(path):
        # logo.png missing? Accept the same name with another image extension.
        stem = os.path.splitext(path)[0]
        path = next((stem + ext for ext in _IMAGE_EXTS if os.path.isfile(stem + ext)), path)
    if not path:
        return None
    size = int(os.getenv("LOGO_SIZE", "220"))
    small = int(os.getenv("LOGO_SMALL_SIZE", "46"))  # the one in front of each car's name
    try:
        logo = {"src": _data_uri(eink_logo_png(path, size)), "size": size,
                "small_src": _data_uri(eink_logo_png(path, small, bold=True)), "small_size": small}
    except Exception as exc:
        log.warning("Could not load logo %s: %s", path, exc)
        return None
    log.info("Logo loaded from %s", path)
    return logo


_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")


def _photo_paths() -> list[str]:
    """Every car image to rotate through: PHOTO_FILE (comma-separated files or a folder),
    otherwise everything in cars/ next to this script, plus a single car.jpg/png if present."""
    here = os.path.dirname(os.path.abspath(__file__))
    entries = [e.strip() for e in os.getenv("PHOTO_FILE", "").split(",") if e.strip()]
    if not entries:
        entries = [os.path.join(here, "cars")] + [os.path.join(here, n) for n in ("car.jpg", "car.jpeg", "car.png")]
    paths: list[str] = []
    for entry in entries:
        if os.path.isdir(entry):
            paths += sorted(os.path.join(entry, n) for n in os.listdir(entry)
                            if n.lower().endswith(_IMAGE_EXTS) and not n.startswith("."))
        elif os.path.isfile(entry):
            paths.append(entry)
    return paths


def _load_photos() -> list[dict[str, Any]]:
    width = min(max(int(os.getenv("PHOTO_WIDTH", "480")), 300), 520)
    focus = os.getenv("PHOTO_FOCUS")
    zoom = float(os.getenv("PHOTO_ZOOM", "1.0"))
    photos = []
    for path in _photo_paths():
        try:
            # Left part of the 800x480 screen (default 60%), below the 44 px header row.
            png = eink_photo_png(path, width, 436, float(focus) if focus else None, zoom)
        except Exception as exc:
            log.warning("Could not load car image %s: %s", path, exc)
            continue
        photos.append({"src": _data_uri(png), "width": width, "height": 436, "file": os.path.basename(path)})
        log.info("Car image loaded: %s (%d bytes)", path, len(png))
    return photos


LOGO: dict[str, Any] | None = None
PHOTOS: list[dict[str, Any]] = []

# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
@contextlib.asynccontextmanager
async def lifespan(_: FastAPI):
    global LOGO, PHOTOS
    LOGO = _load_logo()
    PHOTOS = _load_photos()
    if not BRIDGE_TOKEN:
        log.warning("BRIDGE_TOKEN is not set: anyone on your network can read car data. "
                    "Suggested token: %s", secrets.token_urlsafe(24))
    task = asyncio.create_task(refresher())
    yield
    task.cancel()


app = FastAPI(title="Polestar bridge for LaraPaper", lifespan=lifespan)


@app.middleware("http")
async def collapse_slashes(request: Request, call_next):
    # A Bridge URL saved with a trailing slash yields "//polestar.json"; accept it.
    path = request.scope["path"]
    if "//" in path:
        request.scope["path"] = re.sub("/{2,}", "/", path)
    return await call_next(request)


def _check_token(request: Request) -> None:
    if not BRIDGE_TOKEN:
        return
    header = request.headers.get("authorization", "")
    supplied = header[7:] if header.lower().startswith("bearer ") else request.query_params.get("token", "")
    if not secrets.compare_digest(supplied, BRIDGE_TOKEN):
        raise HTTPException(status_code=401, detail="bad token")


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": state.last_success is not None, "cars": len(state.cars), "error": state.error, "demo": DEMO}


@app.get("/polestar.json")
async def polestar(request: Request, units: str = "imperial") -> JSONResponse:
    _check_token(request)
    imperial = units.lower() not in {"metric", "km"}
    now = datetime.now(timezone.utc)
    names = _display_names(state.cars)
    cars = [format_car(c, names[c.vin], imperial, now) for c in _ordered(state.cars)]
    body = {
        "ok": bool(cars),
        "error": state.error if not cars else None,
        "refresh_error": state.error,
        "demo": DEMO,
        "dist_unit": "mi" if imperial else "km",
        "eff_unit": "mi/kWh" if imperial else "kWh/100 km",
        "updated": _clock(state.last_success, now) if state.last_success else None,
        "car_count": len(cars),
        "cars": cars,
        "logo": LOGO,
        "photo": random.choice(PHOTOS) if PHOTOS else None,  # a different pick on each refresh
    }
    return JSONResponse(body, headers={"Cache-Control": "no-store"})


def _images_payload() -> list[dict[str, Any]]:
    names = _display_names(state.cars)
    return [{"name": names[c.vin], "model": c.model, "model_year": c.model_year,
             "vin_tail": c.vin[-4:], "images": c.images} for c in _ordered(state.cars)]


@app.get("/images.json")
async def images_json(request: Request) -> JSONResponse:
    """Links to Polestar's official images of each of your cars."""
    _check_token(request)
    return JSONResponse({"cars": _images_payload(), "demo": DEMO}, headers={"Cache-Control": "no-store"})


@app.get("/images", response_class=HTMLResponse)
async def images_page(request: Request) -> HTMLResponse:
    """The same links as a page you can open in a browser: /images?token=YOUR_TOKEN"""
    _check_token(request)
    parts = ["<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
             "<title>Polestar images</title><style>body{font:15px system-ui,sans-serif;margin:24px;max-width:1100px}"
             "h2{margin:28px 0 4px}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:14px}"
             "figure{margin:0;border:1px solid #ddd;border-radius:8px;padding:8px;background:"
             "repeating-conic-gradient(#f2f2f2 0 25%,#fff 0 50%) 0 0/16px 16px}"
             "img{width:100%;height:150px;object-fit:contain}figcaption{font-size:13px;margin-top:6px;word-break:break-all}"
             "figcaption a{display:block;margin-top:2px}</style><h1>Polestar images</h1>"]
    cars = _images_payload()
    if DEMO:
        parts.append("<p>Demo mode is on, so there are no real cars to show images for.</p>")
    elif not cars:
        parts.append("<p>No car data yet. The bridge may still be logging in; try again in a minute.</p>")
    for car in cars:
        title = html.escape(f"{car['name']} · {car['model'] or ''} {car['model_year'] or ''}".strip(" ·"))
        parts.append(f"<h2>{title}</h2>")
        if not car["images"]:
            parts.append("<p>Polestar returned no images for this car.</p>")
            continue
        parts.append("<div class='grid'>")
        for img in car["images"]:
            url = html.escape(img["url"], quote=True)
            angle = "default view" if img["angle"] is None else f"angle {img['angle']}°"
            parts.append(f"<figure><a href='{url}' target='_blank' rel='noopener'><img src='{url}' alt='' loading='lazy'></a>"
                         f"<figcaption><b>{angle}</b> · {img['background']} background"
                         f"<a href='{url}' target='_blank' rel='noopener'>{url}</a></figcaption></figure>")
        parts.append("</div>")
    return HTMLResponse("".join(parts), headers={"Cache-Control": "no-store"})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8765")))
