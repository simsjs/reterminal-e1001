"""
Home energy screen (Dominion Energy) for the bridges service (was dominion-bridge).

Uses the community `dompower` library (the one behind the Home Assistant
integration) to read your Dominion Energy Virginia account: the current billing
cycle from the bill-forecast endpoint and 30-minute smart-meter readings. It
refreshes in the background and serves a small, pre-formatted JSON document that
a LaraPaper (TRMNL BYOS) recipe polls. It only reads; it never changes anything
on your account.

    GET /dominion.json      panel data (what the recipe uses)
    GET /raw.json           the untouched bill-forecast response, for checking which fields Dominion sends
    GET /health

Auth: send `Authorization: Bearer <DOMINION_BRIDGE_TOKEN>` (or ?token=...). /health is open.

Login is a one-time step with the dompower CLI inside the bridges container (it
needs the code Dominion texts or emails you):

    docker compose exec bridges dompower login -u you@example.com -o /data/dominion/tokens.json --cookies /data/dominion/cookies.json
    docker compose exec bridges dompower --token-file /data/dominion/tokens.json select-account

After that the bridge keeps the tokens fresh by itself and saves each new pair
back to /data/dominion/tokens.json (both tokens change on every refresh).

Environment:
    DOMINION_TOKEN_FILE default /data/dominion/tokens.json (written by `dompower login`)
    DOMINION_ACCOUNT    optional account number (default: the one picked with select-account,
                        else the default account on your profile)
    DOMINION_METER      optional meter number (same fallbacks)
    DOMINION_BRIDGE_TOKEN       shared secret LaraPaper must send (strongly recommended)
    DOMINION_REFRESH_MINUTES    background refresh interval, default 120. Dominion posts meter
                                data about once a day, so there's no point going much faster.
    DOMINION_KEEPALIVE_MINUTES  how often to rotate the login tokens, default 20. Dominion's
                                refresh token expires about 90 min after the last rotation, so
                                keep this short.
    TZ                  IANA time zone for labels, default America/New_York
    DOMINION_DEMO       "1" to serve realistic fake data without logging in
    DOMINION_LOGO_FILE  image for the top-right corner (default /app/images/dominion/logo.png).
                        Converted to crisp 1-bit at startup.
    DOMINION_LOGO_HEIGHT  logo height on screen in px, default 54 (width follows the image, max 170)
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import logging
import math
import os
import random
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from common import check_token, flag, page_info, setting

NAME = "energy"
log = logging.getLogger("energy")

TZ = ZoneInfo(os.getenv("TZ") or "America/New_York")
TOKEN_FILE = Path(setting("DOMINION", "TOKEN_FILE", "/data/dominion/tokens.json"))
BRIDGE_TOKEN = setting("DOMINION", "BRIDGE_TOKEN")
REFRESH_MINUTES = max(30, int(setting("DOMINION", "REFRESH_MINUTES", "120") or 120))
# Dominion's access token lives 30 min and its refresh token dies roughly an hour after
# that, so the tokens must be rotated far more often than the data is fetched.
KEEPALIVE_MINUTES = max(5, int(setting("DOMINION", "KEEPALIVE_MINUTES", "20") or 20))
DEMO = flag("DOMINION", "DEMO")
HISTORY_DAYS = 9  # enough for 7 full days plus a day or two of reporting lag

# Pixel sizes the recipe draws with (full layout). Kept here so the template stays simple.
DAY_CHART_H = 92   # tallest half-hour bar
WEEK_CHART_H = 64   # tallest daily bar


# ── Logo: trimmed, scaled once and turned into solid black/white for e-ink ──
def _load_logo() -> dict[str, Any] | None:
    candidates = [setting("DOMINION", "LOGO_FILE"), "/app/images/dominion/logo.png"]
    path = next((c for c in candidates if c and Path(c).is_file()), None)
    if not path:
        return None
    try:
        from PIL import Image

        src = Image.open(path).convert("RGBA")
        flat = Image.new("RGBA", src.size, (255, 255, 255, 255))
        flat.alpha_composite(src)
        gray = flat.convert("L")
        bbox = gray.point(lambda v: 255 if v < 245 else 0).getbbox()
        if bbox:
            gray = gray.crop(bbox)
        h = int(setting("DOMINION", "LOGO_HEIGHT", "54") or 54)
        scale = min(170 / gray.width, h / gray.height)
        gray = gray.resize((round(gray.width * scale), round(gray.height * scale)), Image.Resampling.LANCZOS)
        # A hard threshold, not dithering: colored artwork (Dominion blue) goes solid black,
        # which reads far better than a dot pattern at this size.
        bw = gray.point(lambda v: 255 if v > 150 else 0).convert("1")
        buf = io.BytesIO()
        bw.save(buf, format="PNG", optimize=True)
        log.info("Loaded logo %s (%dx%d)", path, bw.width, bw.height)
        return {"src": "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode(), "w": bw.width, "h": bw.height}
    except Exception as err:
        log.warning("Could not load logo %s: %s", path, err)
        return None


LOGO = _load_logo()


# ── Small, library-shaped records so demo data and real data take the same path ──
@dataclass(frozen=True)
class Reading:
    timestamp: datetime
    consumption: float
    generation: float = 0.0


@dataclass(frozen=True)
class Cycle:
    start: date
    end: date
    usage_kwh: float
    last_charges: float
    last_usage: float
    last_start: date | None
    last_end: date | None
    is_tou: bool


# ── Formatting helpers ──
def _day_label(d: date) -> str:
    return f"{d:%a} {d:%b} {d.day}".upper()


def _short_date(d: date) -> str:
    return f"{d:%b} {d.day}"


def _clock(t: datetime) -> str:
    return t.strftime("%I:%M %p").lstrip("0")


def _kwh(x: float) -> str:
    return f"{x:,.0f}" if x >= 100 else f"{x:.1f}"


def _pct(x: float) -> float:
    return round(max(0.0, min(100.0, x)), 1)


# ── The panel ──
MIN_CYCLE_DAYS = 20  # anything shorter is a bad end date, not a real billing cycle


def _parse_any_date(value: Any) -> date | None:
    """Dominion sends dates as "MM/DD/YYYY HH:MM:SS" (sometimes ISO)."""
    if not isinstance(value, str) or not value.strip():
        return None
    v = value.strip()
    for text, fmt in ((v, "%m/%d/%Y %H:%M:%S"), (v.split()[0], "%m/%d/%Y"), (v[:10], "%Y-%m-%d")):
        with contextlib.suppress(ValueError):
            return datetime.strptime(text, fmt).date()
    return None


def fix_cycle_end(cycle: Cycle, raw: dict[str, Any] | None) -> tuple[Cycle, bool]:
    """Make sure the cycle has a believable end date.

    dompower fills a missing end date with *today*, which turns a fresh cycle into
    "day 3 of 3" and wrecks the projection. If the end is missing or too close to the
    start, look for another end date in Dominion's raw response, else assume the cycle
    runs as long as the last bill did. Returns (cycle, end_is_estimated).
    """
    if (cycle.end - cycle.start).days + 1 >= MIN_CYCLE_DAYS:
        return cycle, False
    data = (raw or {}).get("data", {}) if isinstance(raw, dict) else {}
    for key in ("billperdeddate", "billPerdEdDate", "billPeriodEndDate", "nextReadDate", "nextMeterReadDate", "nextBillDate"):
        d = _parse_any_date(data.get(key))
        if d and (d - cycle.start).days + 1 >= MIN_CYCLE_DAYS:
            log.info("Cycle end from raw field %s: %s", key, d)
            return replace(cycle, end=d), False
    length = 30
    if cycle.last_start and cycle.last_end and (cycle.last_end - cycle.last_start).days + 1 >= MIN_CYCLE_DAYS:
        length = (cycle.last_end - cycle.last_start).days + 1
    end = cycle.start + timedelta(days=length - 1)
    log.info("Dominion sent no usable cycle end (got %s); estimating %s", cycle.end, end)
    return replace(cycle, end=end), True


def build_panel(
    cycle: Cycle | None, readings: list[Reading], now: datetime, end_estimated: bool = False
) -> dict[str, Any]:
    """Turn a billing cycle plus 30-minute readings into what the screen shows."""
    by_day: dict[date, list[Reading]] = {}
    for r in readings:
        by_day.setdefault(r.timestamp.astimezone(TZ).date(), []).append(r)

    # The latest day with (nearly) a full set of readings. Dominion usually posts a day late.
    full_days = sorted(d for d, rs in by_day.items() if len(rs) >= 44 and d < now.date())
    panel: dict[str, Any] = {"ok": True, "as_of": _clock(now), "demo": DEMO}
    if LOGO:
        panel["logo"] = LOGO

    latest = full_days[-1] if full_days else None
    avg = None
    if latest:
        rs = sorted(by_day[latest], key=lambda r: r.timestamp)
        total = sum(r.consumption for r in rs)
        week_days = [d for d in full_days if d > latest - timedelta(days=7)]
        week_totals = [sum(r.consumption for r in by_day[d]) for d in week_days]
        avg = sum(week_totals) / len(week_totals)

        top = max(rs, key=lambda r: r.consumption)
        scale = max(0.5, math.ceil(top.consumption * 4) / 4)  # round the y-axis up to the next 0.25 kWh
        # Always-on: the quietest hour of the day, as a steady draw in watts.
        hourly = [rs[i].consumption + rs[i + 1].consumption for i in range(0, len(rs) - 1, 2)]
        base_w = min(hourly) * 1000 if hourly else 0

        if latest == now.date() - timedelta(days=1):
            title = "YESTERDAY"
        else:
            title = "LATEST DAY"
        vs = (total - avg) / avg * 100 if avg else 0
        peak_i = rs.index(top)
        panel["day"] = {
            "title": title,
            "label": _day_label(latest),
            "kwh": _kwh(total),
            "vs_avg": f"{abs(vs):.0f}% {'above' if vs >= 0 else 'below'} 7-day avg" if abs(vs) >= 1 else "right on 7-day avg",
            "peak_kwh": f"{top.consumption:.2f}",
            "peak_at": _clock(top.timestamp.astimezone(TZ)),
            "always_on": f"{base_w:,.0f} W",
            "scale": f"{scale:g}",
            "peak_left": round((peak_i + 0.5) / len(rs) * 100, 1),
            "bars": [
                {"h": max(1, round(r.consumption / scale * DAY_CHART_H)), "peak": r is top}
                for r in rs
            ],
            "stale": latest < now.date() - timedelta(days=2),
        }
        wmax = max(week_totals)
        panel["week"] = {
            "avg": _kwh(avg),
            "days": [
                {
                    "d": f"{d:%a}"[:2],
                    "kwh": f"{t:.0f}",
                    "h": max(2, round(t / wmax * WEEK_CHART_H)),
                    "latest": d == latest,
                }
                for d, t in zip(week_days, week_totals)
            ],
        }
        panel["data_through"] = _short_date(latest)

    if cycle:
        days = (cycle.end - cycle.start).days + 1
        today_n = min(days, max(1, (now.date() - cycle.start).days + 1))
        # Project from the days the meter has actually reported, not from today:
        # kWh so far, plus the recent daily average for each day still to come. Early in a
        # cycle that is far steadier than stretching one or two days across the month.
        reported_through = min(latest or now.date(), cycle.end)
        elapsed = max(1, (reported_through - cycle.start).days + 1)
        remaining = max(0, days - elapsed)
        if avg is not None:
            pace = cycle.usage_kwh + avg * remaining
        else:
            pace = cycle.usage_kwh / elapsed * days
        rate = cycle.last_charges / cycle.last_usage if cycle.last_usage > 0 else None

        span = max(pace, cycle.last_usage, cycle.usage_kwh, 1) * 1.08
        delta = (pace - cycle.last_usage) / cycle.last_usage * 100 if cycle.last_usage else None
        c: dict[str, Any] = {
            "kwh": f"{cycle.usage_kwh:,.0f}",
            "day": today_n,
            "days": days,
            "days_left": max(0, (cycle.end - now.date()).days),
            "range": f"{_short_date(cycle.start)} – {'~' if end_estimated else ''}{_short_date(cycle.end)}",
            "ends": _short_date(cycle.end),
            "pace_kwh": f"{pace:,.0f}",
            "pct_used": _pct(cycle.usage_kwh / span * 100),
            "pct_pace": _pct(pace / span * 100),
            "is_tou": cycle.is_tou,
        }
        if cycle.last_usage:
            c["last_kwh"] = f"{cycle.last_usage:,.0f}"
            c["pct_last"] = _pct(cycle.last_usage / span * 100)
            c["vs_last"] = (
                f"{abs(delta):.0f}% {'more' if delta >= 0 else 'less'} than last bill" if abs(delta) >= 1 else "same as last bill"
            )
            c["over_last"] = delta >= 0
        panel["cycle"] = c

        if rate:
            panel["cost"] = {
                "so_far": f"${cycle.usage_kwh * rate:,.0f}",
                "projected": f"${pace * rate:,.0f}",
                "last": f"${cycle.last_charges:,.2f}",
                "last_period": f"{_short_date(cycle.last_start)} – {_short_date(cycle.last_end)}"
                if cycle.last_start and cycle.last_end
                else "",
                "rate": f"{rate * 100:.1f}¢",
            }

    if "day" not in panel and "cycle" not in panel:
        panel = {"ok": False, "as_of": _clock(now), "error": "Dominion hasn't returned any usage yet."}
    return panel


# ── Demo data: a Northern Virginia house in mid-November ──
def _demo() -> tuple[Cycle, list[Reading]]:
    now = datetime.now(TZ)
    rnd = random.Random(now.date().toordinal())
    cycle_start = now.date() - timedelta(days=20)
    cycle_end = cycle_start + timedelta(days=29)

    def half_hour_kwh(d: date, slot: int, cold: float) -> float:
        h = slot / 2
        v = 0.11  # fridge, router, standby
        if h < 6:
            v += 0.17 * cold  # heat pump through the night
        elif h < 9:
            v += 0.55 + 0.35 * math.exp(-((h - 7.2) ** 2) / 0.6) + 0.25 * cold
        elif h < 16:
            v += 0.18 + 0.12 * cold
        elif h < 22:
            v += 0.42 + 0.75 * math.exp(-((h - 18.6) ** 2) / 1.4) + 0.2 * cold
        else:
            v += 0.25 + 0.25 * cold
        if d.weekday() >= 5 and 9 <= h < 16:
            v += 0.22  # home at the weekend
        return max(0.05, 0.8 * v * rnd.uniform(0.82, 1.2))

    readings: list[Reading] = []
    usage = 0.0
    first = cycle_start - timedelta(days=1)
    last = now.date() - timedelta(days=1)  # meter data a day behind, like the real thing
    d = first
    while d <= last:
        cold = 0.6 + 0.5 * math.sin(d.toordinal() / 2.3) ** 2 + (d - first).days * 0.012
        for slot in range(48):
            kwh = round(half_hour_kwh(d, slot, cold), 2)
            if d == last and slot == 37:
                kwh += 0.9  # dinner + dryer
            ts = datetime(d.year, d.month, d.day, slot // 2, 30 * (slot % 2), tzinfo=TZ)
            if d >= cycle_start:
                usage += kwh
            readings.append(Reading(ts, kwh))
        d += timedelta(days=1)

    cycle = Cycle(
        start=cycle_start,
        end=cycle_end,
        usage_kwh=round(usage),
        last_charges=117.20,
        last_usage=698,
        last_start=cycle_start - timedelta(days=31),
        last_end=cycle_start - timedelta(days=1),
        is_tou=False,
    )
    keep_from = now.date() - timedelta(days=HISTORY_DAYS)
    return cycle, [r for r in readings if r.timestamp.date() >= keep_from]


# ── Live data ──
class Source:
    def __init__(self) -> None:
        self.panel: dict[str, Any] | None = None
        self.raw: dict[str, Any] | None = None
        self.error: str | None = None
        self.last_ok: datetime | None = None
        self.account: str | None = os.getenv("DOMINION_ACCOUNT") or None
        self.meter: str | None = os.getenv("DOMINION_METER") or None

    def _load_tokens(self) -> dict[str, Any]:
        if not TOKEN_FILE.exists():
            raise RuntimeError(f"No tokens yet. Run the one-time `dompower login` (see README); expected {TOKEN_FILE}")
        with TOKEN_FILE.open() as f:
            return json.load(f)

    def _save_tokens(self, access: str, refresh: str) -> None:
        # Both tokens rotate on every refresh; losing the new pair means logging in again.
        data = {}
        with contextlib.suppress(Exception):
            data = self._load_tokens()
        data.update({"access_token": access, "refresh_token": refresh})
        tmp = TOKEN_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(TOKEN_FILE)
        log.info("Saved refreshed Dominion tokens")

    async def keepalive(self) -> None:
        """Rotate the token pair so the refresh token never sits idle long enough to expire."""
        if DEMO or not TOKEN_FILE.exists():
            return
        import aiohttp
        from dompower import DompowerClient

        tokens = self._load_tokens()
        async with aiohttp.ClientSession() as session:
            client = DompowerClient(
                session,
                access_token=tokens.get("access_token"),
                refresh_token=tokens.get("refresh_token"),
                token_update_callback=self._save_tokens,
            )
            await client.async_refresh_tokens()

    async def refresh(self) -> None:
        now = datetime.now(TZ)
        if DEMO:
            cycle, readings = _demo()
            self.panel, self.error, self.last_ok = build_panel(cycle, readings, now), None, now
            return

        import aiohttp
        from dompower import DompowerClient
        from dompower.const import ENDPOINT_BILL_FORECAST

        tokens = self._load_tokens()
        account = self.account or tokens.get("selected_account")
        meter = self.meter or tokens.get("selected_meter")

        async with aiohttp.ClientSession() as session:
            client = DompowerClient(
                session,
                access_token=tokens.get("access_token"),
                refresh_token=tokens.get("refresh_token"),
                token_update_callback=self._save_tokens,
            )
            if not (account and meter):
                info = await client.async_get_customer_info()
                acct = info.default_account
                if not acct or not acct.primary_meter:
                    raise RuntimeError("No active Dominion account with a meter found")
                account, meter = acct.account_number, acct.primary_meter.device_id
                log.info("Using account ending %s", account[-4:])

            fc = await client.async_get_bill_forecast(account)
            with contextlib.suppress(Exception):
                self.raw = await client._async_request(  # noqa: SLF001 - debug view of the same call
                    "GET", ENDPOINT_BILL_FORECAST, params={"accountNumber": account, "actionCode": "4"}
                )
            usage = await client.async_get_interval_usage(
                account, meter, now.date() - timedelta(days=HISTORY_DAYS), now.date()
            )

        cycle, end_estimated = fix_cycle_end(Cycle(
            start=fc.current_period_start,
            end=fc.current_period_end,
            usage_kwh=fc.current_usage_kwh,
            last_charges=fc.last_bill.charges,
            last_usage=fc.last_bill.usage,
            last_start=fc.last_bill.period_start,
            last_end=fc.last_bill.period_end,
            is_tou=fc.is_tou,
        ), self.raw)
        readings = [Reading(u.timestamp, u.consumption, u.generation) for u in usage]
        self.panel, self.error, self.last_ok = build_panel(cycle, readings, now, end_estimated), None, now
        log.info("Refreshed: %s kWh this cycle, %d readings", cycle.usage_kwh, len(readings))


source = Source()
router = APIRouter()


# Both loops rotate the single-use refresh token, so they must never overlap.
_token_lock = asyncio.Lock()


async def _loop() -> None:
    while True:
        try:
            async with _token_lock:
                await source.refresh()
        except asyncio.CancelledError:
            raise
        except Exception as err:  # keep serving the last good data
            source.error = f"{type(err).__name__}: {err}"
            log.warning("Refresh failed: %s", source.error)
        await asyncio.sleep(REFRESH_MINUTES * 60)


async def _keepalive_loop() -> None:
    while True:
        await asyncio.sleep(KEEPALIVE_MINUTES * 60)
        try:
            async with _token_lock:
                await source.keepalive()
        except asyncio.CancelledError:
            raise
        except Exception as err:
            log.warning("Token keep-alive failed: %s: %s", type(err).__name__, err)


def start() -> list:
    if not BRIDGE_TOKEN:
        log.warning("DOMINION_BRIDGE_TOKEN is not set; anyone on your network can read your usage")
    return [_loop(), _keepalive_loop()]


def health() -> dict[str, Any]:
    return {
        "ok": source.error is None and source.panel is not None,
        "demo": DEMO,
        "last_ok": source.last_ok.isoformat() if source.last_ok else None,
        "error": source.error,
    }


@router.get("/dominion.json")
async def dominion_json(request: Request) -> JSONResponse:
    check_token(request, BRIDGE_TOKEN)
    if source.panel:
        out = dict(source.panel)
        if source.error:
            out["warning"] = "Couldn't reach Dominion; showing data from " + out.get("as_of", "earlier")
        return JSONResponse({**out, **page_info(NAME)})
    return JSONResponse({"ok": False, "error": source.error or "Starting up…", **page_info(NAME)})


@router.get("/raw.json")
async def raw_json(request: Request) -> JSONResponse:
    check_token(request, BRIDGE_TOKEN)
    return JSONResponse(source.raw or {"note": "No bill-forecast response yet" + (" (demo mode)" if DEMO else "")})
