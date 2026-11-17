"""Calendar events from private iCal (.ics) links, with a sample week fallback."""
import logging
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta

import icalendar
import recurring_ical_events

import config

log = logging.getLogger("calendar")

_cache: dict[str, tuple[float, bytes]] = {}


@dataclass
class Event:
    title: str
    day: date
    start: datetime | None = None   # None for all-day events
    end: datetime | None = None
    source: str = "main"            # "main" or "brief"
    key: tuple = field(default=())

    @property
    def all_day(self) -> bool:
        return self.start is None


def _fetch(url: str) -> bytes | None:
    url = url.replace("webcal://", "https://", 1)
    hit = _cache.get(url)
    if hit and time.time() - hit[0] < config.CACHE_SECONDS:
        return hit[1]
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "kitchen-board/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read()
        _cache[url] = (time.time(), body)
        return body
    except Exception as exc:
        log.warning("calendar fetch failed (%s…): %s", url[:40], exc)
        return hit[1] if hit else None


def _local(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=config.TZ)
    return dt.astimezone(config.TZ)


def _from_ics(body: bytes, start: date, end: date, source: str) -> list[Event]:
    cal = icalendar.Calendar.from_ical(body)
    out: list[Event] = []
    window_start = datetime.combine(start, dtime.min, config.TZ)
    window_end = datetime.combine(end, dtime.min, config.TZ)
    for comp in recurring_ical_events.of(cal).between(window_start, window_end):
        if str(comp.get("STATUS", "")).upper() == "CANCELLED":
            continue
        title = str(comp.get("SUMMARY", "")).strip() or "(no title)"
        dtstart = comp.get("DTSTART").dt
        dtend_prop = comp.get("DTEND")
        dtend = dtend_prop.dt if dtend_prop is not None else None
        if isinstance(dtstart, datetime):
            s = _local(dtstart)
            e = _local(dtend) if isinstance(dtend, datetime) else s
            out.append(Event(title, s.date(), s, e, source))
        else:  # all-day, possibly spanning several days
            last = (dtend - timedelta(days=1)) if isinstance(dtend, date) else dtstart
            d = dtstart
            while d <= last:
                if start <= d < end:
                    out.append(Event(title, d, None, None, source))
                d += timedelta(days=1)
    return out


def _sample(week_start: date) -> list[Event]:
    """A sample week so the layout shows something before a calendar is linked."""
    def at(day: int, hh: int, mm: int, minutes: int, title: str, source="main") -> Event:
        d = week_start + timedelta(days=day)
        s = datetime.combine(d, dtime(hh, mm), config.TZ)
        return Event(title, d, s, s + timedelta(minutes=minutes), source)

    def allday(day: int, title: str) -> Event:
        return Event(title, week_start + timedelta(days=day))

    return [
        at(0, 6, 30, 45, "Zwift · 45 min"), at(0, 17, 30, 45, "Swim lessons"),
        at(1, 7, 0, 35, "Run · 3.35 mi"), allday(1, "Library day"),
        allday(2, "Veterans Day"), allday(2, "Trash night"),
        at(3, 7, 45, 20, "Drop-off"), at(3, 9, 30, 15, "Team standup", "brief"),
        at(3, 12, 15, 40, "Easy run · 4 mi"), at(3, 15, 0, 30, "Recruiter call", "brief"),
        at(3, 17, 30, 20, "Pickup"),
        at(4, 6, 0, 30, "Lift · 30 min"), allday(4, "Pizza night"),
        at(5, 8, 0, 60, "Long run · 6.5 mi"), allday(5, "Bike ride with the kids"),
        allday(6, "Zwift · 75 min steady"), allday(6, "Meal prep"),
    ]


def using_sample() -> bool:
    if config.SAMPLE_DATA == "on":
        return True
    if config.SAMPLE_DATA == "off":
        return False
    return not (config.CALENDAR_ICS_URLS or config.BRIEF_ONLY_ICS_URLS)


def events_between(start: date, end: date, include_brief_only: bool) -> list[Event]:
    """Events with start <= day < end, deduplicated and sorted (timed first)."""
    if using_sample():
        events, week_start = [], start - timedelta(days=start.weekday())
        while week_start < end:   # the window can cross into next week
            events += [e for e in _sample(week_start) if start <= e.day < end]
            week_start += timedelta(days=7)
    else:
        events = []
        sources = [(u, "main") for u in config.CALENDAR_ICS_URLS]
        if include_brief_only:
            sources += [(u, "brief") for u in config.BRIEF_ONLY_ICS_URLS]
        for url, source in sources:
            body = _fetch(url)
            if body is None:
                continue
            try:
                events += _from_ics(body, start, end, source)
            except Exception as exc:
                log.warning("calendar parse failed: %s", exc)

    if not include_brief_only:
        events = [e for e in events if e.source == "main"]

    seen, unique = set(), []
    for e in events:
        k = (e.title.lower(), e.day, e.start)
        if k not in seen:
            seen.add(k)
            unique.append(e)
    unique.sort(key=lambda e: (e.day, e.all_day, e.start or datetime.min.replace(tzinfo=config.TZ)))
    return unique


def holidays(start: date, end: date) -> list[tuple[date, str]] | None:
    """(day, name) from the public holiday calendar, start <= day < end.

    None if no holiday calendar is set or it has never loaded.
    """
    if config.SAMPLE_DATA == "on":
        sample = [(date(2026, 11, 26), "Thanksgiving Day"), (date(2026, 12, 25), "Christmas Day"),
                  (date(2027, 1, 1), "New Year's Day")]
        return [(d, n) for d, n in sample if start <= d < end]
    if not config.HOLIDAYS_ICS_URL:
        return None
    body = _fetch(config.HOLIDAYS_ICS_URL)
    if body is None:
        return None
    try:
        return [(e.day, e.title) for e in _from_ics(body, start, end, "holiday")]
    except Exception as exc:
        log.warning("holiday calendar parse failed: %s", exc)
        return None
