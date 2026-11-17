"""
Strava -> LaraPaper bridge for the Fitness screen.

Reads your own activities from the Strava API, refreshes in the background, and
serves a small, pre-formatted JSON document that a LaraPaper (TRMNL BYOS) recipe
polls. It only reads; it never changes anything on your Strava account.

    GET /fitness.json       panel data (what the recipe uses)
    GET /health

Auth: send `Authorization: Bearer <BRIDGE_TOKEN>` (or ?token=...). /health is open.

The left side of the screen shows your latest activity in one of three layouts:

    ride     any bike ride: power, heart rate, distance, time, best power and lap power
    route    runs, and walks, hikes or rucks with GPS: a map of the route and mile splits
    effort   everything else (lifting, Whoop workouts): duration, strain, heart-rate chart

The right side shows hours per week for the last 6 full weeks plus this week, split
into total, cycling, running and strength (lifting and rucking together), and this
week's relative effort against the 6-week average.

Strava sign-in is a one-time step on your laptop (see strava/README.md):

    python3 strava/tools/get_strava_token.py CLIENT_ID CLIENT_SECRET

Environment:
    STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET   from strava.com/settings/api
    STRAVA_REFRESH_TOKEN   from get_strava_token.py. Strava can hand out a new one on any
                           refresh; the bridge saves the newest to TOKEN_FILE and prefers it.
    TOKEN_FILE             default /data/strava_tokens.json
    BRIDGE_TOKEN           shared secret LaraPaper must send (strongly recommended)
    REFRESH_MINUTES        background refresh interval, default 15 (minimum 5)
    STRENGTH_TYPES         Strava sport types that count as strength,
                           default WeightTraining,Workout,Crossfit,HIIT
    RUCK_WORDS             words in an activity's title that make it a ruck (counted as
                           strength), default ruck
    TZ                     IANA time zone, default America/New_York
    DEMO                   "1" to serve made-up data without signing in
    DEMO_LAYOUT            with DEMO=1: ride (default), route or effort
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import re
import secrets
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

log = logging.getLogger("strava-bridge")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")

TZ = ZoneInfo(os.getenv("TZ") or "America/New_York")
TOKEN_FILE = Path(os.getenv("TOKEN_FILE", "/data/strava_tokens.json"))
CLIENT_ID = os.getenv("STRAVA_CLIENT_ID", "").strip()
CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET", "").strip()
START_REFRESH_TOKEN = os.getenv("STRAVA_REFRESH_TOKEN", "").strip()
BRIDGE_TOKEN = os.getenv("BRIDGE_TOKEN", "")
REFRESH_MINUTES = max(5, int(os.getenv("REFRESH_MINUTES", "15") or 15))
DEMO = os.getenv("DEMO", "0").strip().lower() in ("1", "true", "yes")
DEMO_LAYOUT = os.getenv("DEMO_LAYOUT", "ride").strip().lower()
STRENGTH_TYPES = {t.strip() for t in os.getenv("STRENGTH_TYPES", "WeightTraining,Workout,Crossfit,HIIT").split(",") if t.strip()}
RUCK_WORDS = [w.strip().lower() for w in os.getenv("RUCK_WORDS", "ruck").split(",") if w.strip()]

API = "https://www.strava.com/api/v3"
WEEKS_BACK = 6  # full weeks of history next to this week

CYCLING_TYPES = {"Ride", "VirtualRide", "GravelRide", "MountainBikeRide", "EBikeRide", "EMountainBikeRide", "Handcycle", "Velomobile"}
RUNNING_TYPES = {"Run", "TrailRun", "VirtualRun"}
FOOT_TYPES = {"Walk", "Hike"}

# Pixel sizes the recipe draws with (full layout). Kept here so the template stays simple.
WEEK_BAR_H = 36   # tallest weekly bar
LAP_CHART_H = 96  # lap power chart
HR_CHART_W, HR_CHART_H = 426, 150
MAP_W, MAP_H = 280, 230

M_PER_MI = 1609.344
FT_PER_M = 3.28084


# ── Formatting helpers ──
def _clock(t: datetime) -> str:
    return t.strftime("%I:%M %p").lstrip("0")


def _day(d: datetime) -> str:
    return f"{d:%a} {d:%b} {d.day}"


def _hms(s: float) -> str:
    s = int(round(s))
    h, m, sec = s // 3600, s % 3600 // 60, s % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _pace(sec_per_mi: float) -> str:
    return _hms(sec_per_mi)


def _miles(m: float) -> str:
    mi = m / M_PER_MI
    return f"{mi:.2f}" if mi < 10 else f"{mi:.1f}"


def _local(a: dict[str, Any]) -> datetime:
    # start_date_local is local wall time written with a Z; drop the zone and attach ours.
    return datetime.fromisoformat(a["start_date_local"].replace("Z", "")).replace(tzinfo=TZ)


def category(a: dict[str, Any]) -> str:
    """cycling, running, strength or other. Rucks count as strength."""
    t = a.get("sport_type") or a.get("type") or ""
    name = (a.get("name") or "").lower()
    if t in CYCLING_TYPES:
        return "cycling"
    if t in RUNNING_TYPES:
        return "running"
    if t in STRENGTH_TYPES or any(w in name for w in RUCK_WORDS):
        return "strength"
    return "other"


def layout(a: dict[str, Any]) -> str:
    cat = category(a)
    if cat == "cycling":
        return "ride"
    has_route = bool((a.get("map") or {}).get("summary_polyline")) and (a.get("distance") or 0) > 400
    if cat == "running" and has_route:
        return "route"
    if has_route and (a.get("sport_type") in FOOT_TYPES or cat == "strength") and a.get("sport_type") != "WeightTraining":
        return "route"
    return "effort"


# ── Polyline and charts ──
def decode_polyline(p: str) -> list[tuple[float, float]]:
    pts, i, lat, lng = [], 0, 0, 0
    while i < len(p):
        vals = []
        for _ in range(2):
            shift = result = 0
            while True:
                b = ord(p[i]) - 63
                i += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            vals.append(~(result >> 1) if result & 1 else result >> 1)
        lat += vals[0]
        lng += vals[1]
        pts.append((lat / 1e5, lng / 1e5))
    return pts


def route_map(polyline: str, w: int = MAP_W, h: int = MAP_H, pad: int = 14) -> dict[str, Any] | None:
    """Project a route into a w×h box: an SVG path, start and finish points, and a scale bar."""
    pts = decode_polyline(polyline)
    if len(pts) < 2:
        return None
    lat0 = sum(p[0] for p in pts) / len(pts)
    kx = math.cos(math.radians(lat0)) * 111_320  # metres per degree of longitude here
    ky = 110_574
    xs = [p[1] * kx for p in pts]
    ys = [p[0] * ky for p in pts]
    span_x, span_y = max(xs) - min(xs) or 1, max(ys) - min(ys) or 1
    top = 22  # room for the scale bar and legend
    scale = min((w - 2 * pad) / span_x, (h - pad - top) / span_y)  # px per metre
    ox = (w - span_x * scale) / 2
    oy = top + (h - top - pad - span_y * scale) / 2
    xy = [(ox + (x - min(xs)) * scale, oy + (max(ys) - y) * scale) for x, y in zip(xs, ys)]
    path = "M" + " L".join(f"{x:.1f} {y:.1f}" for x, y in xy)
    # A round scale-bar length that fits in about a quarter of the width.
    target = w * 0.25 / scale
    nice = next((n for n in (50, 100, 200, 250, 500, 1000, 2000, 5000) if n >= target * 0.6), 5000)
    label = f"{nice // 1000} KM" if nice >= 1000 else f"{nice} M"
    return {
        "w": w,
        "h": h,
        "path": path,
        "start": {"x": round(xy[0][0], 1), "y": round(xy[0][1], 1)},
        "end": {"x": round(xy[-1][0] - 5, 1), "y": round(xy[-1][1] - 5, 1)},
        "scale_px": round(nice * scale, 1),
        "scale_label": label,
    }


def area_path(values: list[float], w: int, h: int, lo: float, hi: float) -> str:
    n = len(values)
    if n < 2:
        return ""
    y = lambda v: h - (min(max(v, lo), hi) - lo) / (hi - lo) * (h - 4)  # noqa: E731
    pts = " ".join(f"L{i / (n - 1) * w:.1f} {y(v):.1f}" for i, v in enumerate(values))
    return f"M0 {h} {pts} L{w} {h} Z"


def downsample(values: list[float], n: int = 120) -> list[float]:
    if len(values) <= n:
        return values
    step = len(values) / n
    return [sum(values[int(i * step) : int((i + 1) * step)]) / max(1, int((i + 1) * step) - int(i * step)) for i in range(n)]


def best_power(watts: list[float], times: list[float]) -> list[dict[str, Any]]:
    """Best average power for 5 s, 1, 5 and 20 minutes, from a watts stream."""
    if not watts:
        return []
    # Resample to one value per second so gaps don't shorten a window.
    if times and len(times) == len(watts):
        end = int(times[-1])
        per_s = [0.0] * (end + 1)
        j = 0
        for s in range(end + 1):
            while j + 1 < len(times) and times[j + 1] <= s:
                j += 1
            per_s[s] = watts[j] or 0.0
    else:
        per_s = [w or 0.0 for w in watts]
    pref = [0.0]
    for v in per_s:
        pref.append(pref[-1] + v)
    out = []
    for secs, label in ((5, "5 sec"), (60, "1 min"), (300, "5 min"), (1200, "20 min")):
        if len(per_s) < secs:
            continue
        best = max(pref[i + secs] - pref[i] for i in range(len(per_s) - secs + 1)) / secs
        out.append({"label": label, "value": f"{best:.0f}", "unit": "W"})
    return out


def strain(description: str) -> tuple[str | None, str | None]:
    """Whoop writes '12.5 Strain amounts to moderate exertion today.' into the description."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*Strain amounts to (\w+) exertion", description or "", re.I)
    return (m.group(1), m.group(2).lower()) if m else (None, None)


# ── The panel ──
def tile(big: str, unit: str, cap: str) -> dict[str, str]:
    return {"big": big, "unit": unit, "cap": cap}


def build_last(a: dict[str, Any], detail: dict[str, Any], streams: dict[str, list], laps: list[dict], recent: list[dict]) -> dict[str, Any]:
    kind = layout(a)
    start = _local(a)
    sport = a.get("sport_type") or a.get("type") or "Activity"
    name = a.get("name") or sport
    desc = detail.get("description") or ""
    hr_avg, hr_max = a.get("average_heartrate"), a.get("max_heartrate")
    is_zwift = name.lower().startswith("zwift") or "zwift" in (detail.get("device_name") or "").lower()
    out: dict[str, Any] = {"kind": kind, "when": _day(start), "time": _clock(start)}
    extras: list[dict[str, Any]] = []

    if kind == "ride":
        title = re.sub(r"^zwift\s*[-–:]\s*", "", name, flags=re.I)
        title = re.sub(r"^JOIN:\s*", "", title)
        place = ""
        m = re.search(r"🗺️?\s*(.+?) in ([^\n]+)", desc)
        if m:
            place = f"{m.group(1).strip()} · {m.group(2).strip()}"
        else:
            m = re.match(r"(.+?) in ([A-Z][\w' ]+)$", title)
            if m and is_zwift:
                title, place = m.group(1), m.group(2)
        sub = [p for p in (place, "JOIN workout" if "JOIN workout" in desc else "") if p]
        out.update(
            label=f"Last ride · {_day(start)}",
            corner="Zwift" if is_zwift else out["time"],
            title=title,
            sub=" · ".join(sub) or ("Indoor ride" if a.get("trainer") else sport),
        )
        tiles = []
        if a.get("average_watts"):
            tiles.append(tile(f"{a['average_watts']:.0f}", "W", "avg power"))
        if hr_avg:
            tiles.append(tile(f"{hr_avg:.0f}", "bpm", "avg heart rate"))
        tiles.append(tile(_miles(a.get("distance") or 0), "mi", "distance"))
        tiles.append(tile(_hms(a.get("moving_time") or 0), "", "moving time"))
        if len(tiles) < 4 and a.get("average_speed"):
            tiles.insert(2, tile(f"{a['average_speed'] * 2.23694:.1f}", "mph", "avg speed"))
        out["tiles"] = tiles[:4]
        if hr_max:
            extras.append({"t": f"Max HR {hr_max:.0f}"})
        extras.append({"t": f"{(a.get('total_elevation_gain') or 0) * FT_PER_M:,.0f} ft climb"})
        if a.get("average_cadence"):
            extras.append({"t": f"{a['average_cadence']:.0f} rpm"})
        if detail.get("calories"):
            extras.append({"t": f"{detail['calories']:.0f} cal"})
        if a.get("pr_count"):
            extras.append({"t": f"{a['pr_count']} PR{'s' if a['pr_count'] != 1 else ''}", "b": True})

        bp = best_power(streams.get("watts") or [], streams.get("time") or [])
        if bp:
            out["best"] = bp
        lap_w = [(lp.get("moving_time") or lp.get("elapsed_time") or 0, lp.get("average_watts") or 0) for lp in laps]
        if len(lap_w) >= 2 and max(w for _, w in lap_w) > 0:
            top = max(w for _, w in lap_w)
            out["laps"] = {
                "peak": f"{top:.0f} W",
                "h": LAP_CHART_H,
                "bars": [{"s": max(1, s), "pct": max(2, round(w / top * 100))} for s, w in lap_w],
            }
        elif streams.get("heartrate"):
            out["hr"] = hr_chart(streams, hr_max)

    elif kind == "route":
        cat = category(a)
        what = "run" if cat == "running" else ("ruck" if cat == "strength" else sport.lower())
        out.update(
            label=f"Last {what} · {_day(start)}",
            corner=out["time"],
            title=name,
            sub=" · ".join(p for p in (detail.get("location_city") or "", workout_kind(desc)) if p) or sport,
        )
        dist = a.get("distance") or 0
        mt = a.get("moving_time") or 0
        tiles = [tile(_miles(dist), "mi", "distance")]
        if dist > 0:
            tiles.append(tile(_pace(mt / (dist / M_PER_MI)), "/mi", "avg pace"))
        tiles.append(tile(_hms(mt), "", "moving time"))
        if hr_avg:
            tiles.append(tile(f"{hr_avg:.0f}", "bpm", "avg heart rate"))
        out["tiles"] = tiles
        out["map"] = route_map((a.get("map") or {}).get("summary_polyline") or "")
        splits = [
            s for s in (detail.get("splits_standard") or []) if (s.get("distance") or 0) >= M_PER_MI * 0.97
        ][:4]
        out["splits"] = [{"label": f"Mi {i + 1}", "value": _pace(s.get("moving_time") or s.get("elapsed_time") or 0)} for i, s in enumerate(splits)]
        notes = []
        if hr_max:
            notes.append({"t": f"Max HR {hr_max:.0f}"})
        notes.append({"t": f"{(a.get('total_elevation_gain') or 0) * FT_PER_M:,.0f} ft climb"})
        for be in detail.get("best_efforts") or []:
            if be.get("name") in ("1 mile", "5k"):
                notes.append({"t": "Best mile" if be["name"] == "1 mile" else "Best 5K", "v": _hms(be.get("elapsed_time") or 0)})
        out["notes"] = notes

    else:  # effort
        level_val, level = strain(desc)
        cat = category(a)
        label = "lift" if sport == "WeightTraining" else ("ruck" if cat == "strength" and sport != "Workout" else "workout")
        out.update(
            label=f"Last {label} · {_day(start)}",
            corner=out["time"],
            title=name,
            sub=" · ".join(p for p in ((f"{level.capitalize()} exertion" if level else ""), ("from Whoop" if level_val else sport)) if p),
        )
        tiles = [tile(_hms(a.get("elapsed_time") or a.get("moving_time") or 0), "", "duration")]
        if level_val:
            tiles.append(tile(level_val, "", "Whoop strain"))
        if hr_avg:
            tiles.append(tile(f"{hr_avg:.0f}", "bpm", "avg heart rate"))
        if detail.get("calories"):
            tiles.append(tile(f"{detail['calories']:.0f}", "cal", "calories"))
        if len(tiles) < 4 and a.get("distance"):
            tiles.append(tile(_miles(a["distance"]), "mi", "distance"))
        out["tiles"] = tiles[:4]
        if hr_max:
            extras.append({"t": f"Max HR {hr_max:.0f}"})
        if a.get("suffer_score"):
            extras.append({"t": f"Rel. effort {a['suffer_score']:.0f}"})
        cutoff = datetime.now(TZ) - timedelta(days=30)
        month = [x for x in recent if _local(x) >= cutoff and category(x) == "strength"]
        lifts = sum(1 for x in month if x.get("sport_type") == "WeightTraining")
        others = len(month) - lifts
        if month:
            parts = [f"{lifts} lift{'s' if lifts != 1 else ''}"] if lifts else []
            if others:
                parts.append(f"{others} ruck{'s' if others != 1 else ''}")
            extras.append({"t": "Last 30 days: ", "v": " · ".join(parts)})
        if streams.get("heartrate"):
            out["hr"] = hr_chart(streams, hr_max)
            if cat == "strength":
                out["hr"]["label"] = "Heart rate · spikes are sets"

    out["extras"] = extras
    return out


def workout_kind(desc: str) -> str:
    m = re.search(r"Detected workout type:\s*(\w+)", desc or "")
    return m.group(1) if m else ""


def hr_chart(streams: dict[str, list], hr_max: float | None) -> dict[str, Any]:
    hr = [float(v) for v in streams.get("heartrate") or [] if v]
    times = streams.get("time") or []
    vals = downsample(hr, 120)
    lo, hi = 50.0, max(155.0, (hr_max or 0) + 5)
    mins = (times[-1] / 60) if times else len(hr) / 60
    step = 10 if mins > 25 else 5
    ticks = [{"pct": round(m / mins * 100, 1), "t": f"{m}" + (" MIN" if i == 1 else "")} for i, m in enumerate(range(0, int(mins) + 1, step))]
    guide = 120 if hi > 125 else 100
    return {
        "w": HR_CHART_W,
        "h": HR_CHART_H,
        "path": area_path(vals, HR_CHART_W, HR_CHART_H, lo, hi),
        "guide_y": round(HR_CHART_H - (guide - lo) / (hi - lo) * (HR_CHART_H - 4), 1),
        "guide": guide,
        "peak": f"{max(hr):.0f} bpm" if hr else "",
        "label": "Heart rate",
        "ticks": ticks,
    }


def build_weeks(acts: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    this_monday = (now - timedelta(days=now.weekday())).date()
    weeks = [this_monday - timedelta(weeks=k) for k in range(WEEKS_BACK, -1, -1)]
    hours = {w: {"total": 0.0, "cycling": 0.0, "running": 0.0, "strength": 0.0} for w in weeks}
    effort = {w: 0.0 for w in weeks}
    for a in acts:
        d = _local(a).date()
        wk = d - timedelta(days=d.weekday())
        if wk not in hours:
            continue
        h = (a.get("moving_time") or a.get("elapsed_time") or 0) / 3600
        hours[wk]["total"] += h
        cat = category(a)
        if cat in hours[wk]:
            hours[wk][cat] += h
        effort[wk] += a.get("suffer_score") or 0

    top = max((hours[w]["total"] for w in weeks), default=0) or 1
    rows = []
    for key, name in (("total", "Total"), ("cycling", "Cycling"), ("running", "Running"), ("strength", "Strength")):
        vals = [hours[w][key] for w in weeks]
        avg = sum(vals[:-1]) / WEEKS_BACK
        rows.append(
            {
                "key": key,
                "name": name,
                "now": f"{vals[-1]:.1f}",
                "avg": f"{avg:.1f}",
                "bars": [{"h": round(v / top * WEEK_BAR_H), "now": i == len(vals) - 1} for i, v in enumerate(vals)],
            }
        )
    eff = [effort[w] for w in weeks]
    out: dict[str, Any] = {
        "rows": rows,
        "bar_h": WEEK_BAR_H,
        "labels": [f"{w.month}/{w.day}" for w in weeks[:-1]] + ["NOW"],
    }
    if any(eff):
        out["effort"] = {"now": f"{eff[-1]:.0f}", "avg": f"{sum(eff[:-1]) / WEEKS_BACK:.0f}"}
    return out


def build_panel(acts: list[dict[str, Any]], last: dict[str, Any] | None, now: datetime) -> dict[str, Any]:
    panel: dict[str, Any] = {"ok": True, "as_of": _clock(now), "demo": DEMO}
    panel["weeks"] = build_weeks(acts, now)
    if last:
        panel["last"] = last
    if not acts:
        panel = {"ok": False, "as_of": _clock(now), "error": "Strava hasn't returned any activities yet."}
    return panel


# ── Demo data (made up, nowhere real) ──
def _demo(now: datetime) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rnd = random.Random(now.date().toordinal())
    monday = (now - timedelta(days=now.weekday())).replace(hour=15, minute=0, second=0, microsecond=0)

    def act(days_ago: float, sport: str, mins: float, effort: int, **kw: Any) -> dict[str, Any]:
        t = monday + timedelta(days=-days_ago)
        a = {
            "id": int(t.timestamp()),
            "name": kw.pop("name", f"Afternoon {sport}"),
            "sport_type": sport,
            "start_date_local": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "moving_time": int(mins * 60),
            "elapsed_time": int(mins * 60),
            "suffer_score": effort,
            "distance": 0,
        }
        a.update(kw)
        return a

    acts = []
    for wk in range(WEEKS_BACK, 0, -1):
        base = wk * 7
        acts.append(act(base - 1, "VirtualRide", rnd.uniform(25, 40), rnd.randint(12, 30), distance=14000))
        acts.append(act(base - 3, "VirtualRide", rnd.uniform(25, 55), rnd.randint(14, 60), distance=16000))
        if wk % 2 == 0:
            acts.append(act(base - 2, "Run", rnd.uniform(18, 40), rnd.randint(25, 70), distance=5200))
        if wk in (6, 2):
            acts.append(act(base - 4, "WeightTraining", rnd.uniform(36, 42), 6))
        acts.append(act(base - 5, "Walk", 25, 2, name="Lunch ruck", distance=1900))

    ride = act(-0.0, "VirtualRide", 55.6, 20, name="Zwift - JOIN: Burgundy Rollers – Short", distance=23370,
               average_watts=152, average_heartrate=124, max_heartrate=161, total_elevation_gain=209,
               average_cadence=86, pr_count=4, average_speed=7.0, trainer=True)
    acts.append(ride)

    if DEMO_LAYOUT == "route":
        run = act(-0.0, "Run", 25.9, 10, name="Afternoon Run", distance=5399, average_heartrate=134, max_heartrate=168,
                  total_elevation_gain=51, map={"summary_polyline": demo_route_polyline()})
        acts.append(run)
        detail = {
            "description": "Detected workout type: Endurance",
            "splits_standard": [{"distance": 1609.3, "moving_time": t} for t in (458, 457, 474)] + [{"distance": 571, "moving_time": 169}],
            "best_efforts": [{"name": "1 mile", "elapsed_time": 449}, {"name": "5k", "elapsed_time": 1430}],
        }
        return acts, build_last(run, detail, {}, [], acts)

    if DEMO_LAYOUT == "effort":
        lift = act(-0.0, "WeightTraining", 40.5, 5, name="Afternoon Weight Training", average_heartrate=91, max_heartrate=150)
        acts.append(lift)
        hr, times = [], []
        for i in range(2431):
            sets = 0.5 + 0.5 * math.sin(i / 2431 * 2 * math.pi * 14)
            hr.append(70 + 15 * i / 2431 + 38 * sets ** 6 + rnd.uniform(-4, 4) + (40 if 2080 < i < 2110 else 0))
            times.append(i)
        detail = {"description": "12.5 Strain amounts to moderate exertion today.", "calories": 81}
        return acts, build_last(lift, detail, {"heartrate": hr, "time": times}, [], acts)

    # A 55-minute ramp workout: warm-up, three hard minutes, cool-down.
    plan = [(120, 122), (600, 106), (780, 153), (180, 200), (60, 222), (180, 199), (60, 222), (180, 200), (60, 222), (120, 129), (780, 152), (219, 127)]
    watts, times, t = [], [], 0
    for secs, w in plan:
        for _ in range(secs):
            watts.append(w + rnd.uniform(-25, 25))
            times.append(t)
            t += 1
    laps = [{"moving_time": s, "average_watts": w} for s, w in plan]
    detail = {"description": "🗺️ Jungle Circuit in Watopia\n\n📊 JOIN workout", "calories": 484, "device_name": "Zwift"}
    last = build_last(ride, detail, {"watts": watts, "time": times}, laps, acts)
    return acts, last


def demo_route_polyline() -> str:
    """A made-up loop for the run preview (two lobes, like a figure of eight)."""
    pts = []
    for i in range(160):
        a = i / 159 * 2 * math.pi
        r = 0.0042 * (1 + 0.35 * math.sin(3 * a))
        pts.append((40.0 + r * math.sin(2 * a) * 0.8, -100.0 + r * math.cos(a) * 1.3))
    out, plat, plng = [], 0, 0
    for lat, lng in pts:
        ilat, ilng = round(lat * 1e5), round(lng * 1e5)
        for v in (ilat - plat, ilng - plng):
            v = ~(v << 1) if v < 0 else v << 1
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1F)) + 63))
                v >>= 5
            out.append(chr(v + 63))
        plat, plng = ilat, ilng
    return "".join(out)


# ── Live data ──
class Source:
    def __init__(self) -> None:
        self.panel: dict[str, Any] | None = None
        self.error: str | None = None
        self.last_ok: datetime | None = None
        self._access: str | None = None
        self._expires = 0.0
        self._cache: dict[int, tuple[str, dict, dict, list]] = {}  # activity id -> details, fetched once

    # Tokens
    def _refresh_token(self) -> str:
        if TOKEN_FILE.exists():
            try:
                saved = json.loads(TOKEN_FILE.read_text())
                # A new STRAVA_REFRESH_TOKEN in .env (after signing in again) wins over the saved one.
                if saved.get("refresh_token") and (saved.get("seed") == START_REFRESH_TOKEN or not START_REFRESH_TOKEN):
                    return saved["refresh_token"]
            except Exception as err:
                log.warning("Could not read %s: %s", TOKEN_FILE, err)
        if not START_REFRESH_TOKEN:
            raise RuntimeError("No Strava sign-in yet. Set STRAVA_REFRESH_TOKEN (see strava/README.md).")
        return START_REFRESH_TOKEN

    async def _token(self, client: httpx.AsyncClient) -> str:
        if self._access and time.time() < self._expires - 120:
            return self._access
        if not (CLIENT_ID and CLIENT_SECRET):
            raise RuntimeError("Set STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET (see strava/README.md).")
        r = await client.post(
            "https://www.strava.com/oauth/token",
            data={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token(),
            },
        )
        if r.status_code != 200:
            raise RuntimeError(f"Strava sign-in failed ({r.status_code}): {r.text[:200]}")
        data = r.json()
        self._access, self._expires = data["access_token"], float(data.get("expires_at") or 0)
        try:
            TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = TOKEN_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps({"refresh_token": data["refresh_token"], "seed": START_REFRESH_TOKEN}))
            tmp.replace(TOKEN_FILE)
        except Exception as err:
            log.warning("Could not save the Strava refresh token: %s", err)
        return self._access

    async def _get(self, client: httpx.AsyncClient, path: str, **params: Any) -> Any:
        token = await self._token(client)
        r = await client.get(API + path, params=params, headers={"Authorization": f"Bearer {token}"})
        if r.status_code == 429:
            raise RuntimeError("Strava rate limit reached; will try again later")
        r.raise_for_status()
        return r.json()

    async def refresh(self) -> None:
        now = datetime.now(TZ)
        if DEMO:
            acts, last = _demo(now)
            self.panel, self.error, self.last_ok = build_panel(acts, last, now), None, now
            return

        this_monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        after = int((this_monday - timedelta(weeks=WEEKS_BACK, days=1)).timestamp())
        async with httpx.AsyncClient(timeout=30) as client:
            acts: list[dict[str, Any]] = []
            for page in range(1, 4):
                batch = await self._get(client, "/athlete/activities", after=after, per_page=100, page=page)
                acts.extend(batch)
                if len(batch) < 100:
                    break
            acts.sort(key=lambda a: a.get("start_date") or "")

            last = None
            if acts:
                a = acts[-1]
                key = (a.get("name"), a.get("moving_time"))
                cached = self._cache.get(a["id"])
                if not cached or cached[0] != repr(key):
                    detail = await self._get(client, f"/activities/{a['id']}")
                    kind = layout(a)
                    keys = "time,heartrate,watts" if kind == "ride" else "time,heartrate"
                    try:
                        raw = await self._get(client, f"/activities/{a['id']}/streams", keys=keys, key_by_type="true")
                        streams = {k: v.get("data") or [] for k, v in raw.items()}
                    except httpx.HTTPStatusError:
                        streams = {}
                    laps = detail.get("laps") or []
                    if kind == "ride" and not laps:
                        laps = await self._get(client, f"/activities/{a['id']}/laps")
                    self._cache = {a["id"]: (repr(key), detail, streams, laps)}
                _, detail, streams, laps = self._cache[a["id"]]
                last = build_last(a, detail, streams, laps, acts)

        self.panel, self.error, self.last_ok = build_panel(acts, last, now), None, now
        log.info("Refreshed: %d activities, latest %s", len(acts), (last or {}).get("label", "none"))


source = Source()
app = FastAPI(title="strava-bridge")


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
    if not BRIDGE_TOKEN:
        log.warning("BRIDGE_TOKEN is not set; anyone on your network can read your activities")
    asyncio.create_task(_loop())


def _check(request: Request) -> None:
    if not BRIDGE_TOKEN:
        return
    got = request.headers.get("authorization", "").removeprefix("Bearer ").strip() or request.query_params.get("token", "")
    if not secrets.compare_digest(got, BRIDGE_TOKEN):
        raise HTTPException(401, "bad token")


@app.get("/fitness.json")
async def fitness_json(request: Request) -> JSONResponse:
    _check(request)
    if source.panel:
        out = dict(source.panel)
        if source.error:
            out["warning"] = "Couldn't reach Strava; showing data from " + out.get("as_of", "earlier")
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
