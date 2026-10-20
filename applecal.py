"""Read-only Apple Calendar (iCloud CalDAV) sync.

Turns your calendar into the same busy lines and travel days you'd otherwise
paste by hand. Callie never writes to your calendar.

Timed events become busy blocks. All-day events count as travel days only when
their title says so (travel, trip, flight, vacation, OOO, out of town, away);
other all-day events (birthdays, holidays) are ignored. Events marked Free are
skipped, and so are cancelled ones.
"""
import os
import re
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

PACIFIC = ZoneInfo("America/Los_Angeles")
ICLOUD_URL = "https://caldav.icloud.com"
TRAVEL_RE = re.compile(r"\b(travel|traveling|trip|flight|flying|vacation|ooo|out of (?:the )?office|out of town|away)\b", re.I)
CACHE_SECONDS = 300

_cache = {"at": 0.0, "key": None, "data": None}


class CalendarError(Exception):
    pass


def _env(name):
    """Railway variable, upper or lower case."""
    return os.environ.get(name) or os.environ.get(name.lower()) or ""


def configured():
    return bool(_env("ICLOUD_USER") and _env("ICLOUD_APP_PASSWORD"))


def _clean(text):
    return re.sub(r"\s+", " ", str(text or "")).strip() or "Busy"


def _as_pacific(value):
    """datetime (aware or floating) -> Pacific datetime."""
    if value.tzinfo is None:
        return value.replace(tzinfo=PACIFIC)
    return value.astimezone(PACIFIC)


def _hhmm(dt, day):
    if dt.date() > day:
        return "24:00"
    return "%02d:%02d" % (dt.hour, dt.minute)


def blocks_from_events(events, first, last):
    """events: icalendar VEVENT components (already expanded).
    Returns (busy_lines, travel_dates) for days first..last inclusive."""
    busy, travel = set(), set()
    for ev in events:
        if str(ev.get("TRANSP", "")).upper() == "TRANSPARENT" or str(ev.get("STATUS", "")).upper() == "CANCELLED":
            continue
        if ev.get("DTSTART") is None:
            continue
        title = _clean(ev.get("SUMMARY"))
        start = ev.decoded("DTSTART")
        if ev.get("DTEND") is not None:
            end = ev.decoded("DTEND")
        elif ev.get("DURATION") is not None:
            end = start + ev.decoded("DURATION")
        else:
            end = start + (timedelta(days=1) if not isinstance(start, datetime) else timedelta(0))

        if not isinstance(start, datetime):  # all-day
            if not TRAVEL_RE.search(title):
                continue
            end = end if not isinstance(end, datetime) else end.date()
            day = max(start, first)
            while day < max(end, start + timedelta(days=1)) and day <= last:
                travel.add(day)
                day += timedelta(days=1)
            continue

        start, end = _as_pacific(start), _as_pacific(end)
        if end <= start:
            continue
        day = max(start.date(), first)
        while day <= min(end.date(), last):
            day_start = datetime(day.year, day.month, day.day, tzinfo=PACIFIC)
            s = max(start, day_start)
            e = min(end, day_start + timedelta(days=1))
            if e > s:
                busy.add("%s %s-%s %s" % (day.isoformat(), _hhmm(s, day), _hhmm(e, day), title))
            day += timedelta(days=1)
    return sorted(busy), sorted(travel)


def _fetch_events(first, last):
    import caldav  # imported here so the app runs without it when the calendar isn't connected

    wanted = {n.strip().lower() for n in _env("ICLOUD_CALENDARS").split(",") if n.strip()}
    start = datetime(first.year, first.month, first.day, tzinfo=PACIFIC)
    end = datetime(last.year, last.month, last.day, tzinfo=PACIFIC) + timedelta(days=1)
    try:
        with caldav.DAVClient(url=ICLOUD_URL, username=_env("ICLOUD_USER").strip(),
                              password=_env("ICLOUD_APP_PASSWORD").strip(), timeout=20) as client:
            events = []
            for cal in client.principal().calendars():
                name = (cal.name or "").strip()
                if wanted and name.lower() not in wanted:
                    continue
                try:
                    found = cal.search(start=start, end=end, event=True, expand=True)
                except Exception:
                    continue  # reminders lists and other non-event calendars refuse event searches
                for obj in found:
                    events.extend(c for c in obj.icalendar_instance.walk("VEVENT"))
            return events
    except caldav.lib.error.AuthorizationError:
        raise CalendarError("Apple Calendar refused the sign-in. Check ICLOUD_USER and the app-specific password.")
    except Exception as e:
        raise CalendarError("Couldn't reach Apple Calendar (%s). Your rules still work with pasted blocks." % type(e).__name__)


def sync(today, days, refresh=False):
    """Busy lines and travel days from tomorrow through today + days."""
    first, last = today + timedelta(days=1), today + timedelta(days=days)
    key = (first, last)
    if not refresh and _cache["key"] == key and time.time() - _cache["at"] < CACHE_SECONDS:
        return _cache["data"]
    busy, travel = blocks_from_events(_fetch_events(first, last), first, last)
    data = {"busy": busy, "travel": [d.isoformat() for d in travel],
            "synced_at": datetime.now(PACIFIC).strftime("%-I:%M %p")}
    _cache.update(at=time.time(), key=key, data=data)
    return data


def self_test():
    """Synthetic events through blocks_from_events. Returns {passed, total, failures}."""
    from icalendar import Event

    def ev(summary, start, end, **extra):
        e = Event()
        e.add("SUMMARY", summary)
        e.add("DTSTART", start)
        e.add("DTEND", end)
        for k, v in extra.items():
            e.add(k, v)
        return e

    ny = ZoneInfo("America/New_York")
    first, last = date(2026, 10, 20), date(2026, 11, 2)
    events = [
        ev("Class", datetime(2026, 10, 20, 9, 0, tzinfo=PACIFIC), datetime(2026, 10, 20, 12, 0, tzinfo=PACIFIC)),
        ev("Call with NY", datetime(2026, 10, 21, 15, 0, tzinfo=ny), datetime(2026, 10, 21, 16, 0, tzinfo=ny)),
        ev("Drinks with\nSam", datetime(2026, 10, 22, 18, 0), datetime(2026, 10, 22, 20, 0)),
        ev("Red-eye", datetime(2026, 10, 23, 22, 0, tzinfo=PACIFIC), datetime(2026, 10, 24, 6, 0, tzinfo=PACIFIC)),
        ev("Trip to Austin", date(2026, 10, 27), date(2026, 10, 29)),
        ev("Mom's birthday", date(2026, 10, 30), date(2026, 10, 31)),
        ev("Focus time", datetime(2026, 10, 26, 9, 0, tzinfo=PACIFIC), datetime(2026, 10, 26, 10, 0, tzinfo=PACIFIC), TRANSP="TRANSPARENT"),
        ev("Old meeting", datetime(2026, 10, 19, 9, 0, tzinfo=PACIFIC), datetime(2026, 10, 19, 10, 0, tzinfo=PACIFIC)),
    ]
    busy, travel = blocks_from_events(events, first, last)
    cases = [
        ("C1", "Pacific event kept as is", "2026-10-20 09:00-12:00 Class" in busy),
        ("C2", "Eastern event shown in Pacific", "2026-10-21 12:00-13:00 Call with NY" in busy),
        ("C3", "Floating time read as Pacific, newlines cleaned", "2026-10-22 18:00-20:00 Drinks with Sam" in busy),
        ("C4", "Overnight event split at midnight",
         "2026-10-23 22:00-24:00 Red-eye" in busy and "2026-10-24 00:00-06:00 Red-eye" in busy),
        ("C5", "Travel all-day event becomes travel days", travel == [date(2026, 10, 27), date(2026, 10, 28)]),
        ("C6", "Other all-day events ignored", not any("birthday" in b for b in busy)),
        ("C7", "Free events skipped", not any("Focus" in b for b in busy)),
        ("C8", "Days outside the range skipped", not any(b.startswith("2026-10-19") for b in busy)),
    ]
    failures = [{"id": c[0], "name": c[1]} for c in cases if not c[2]]
    return {"passed": len(cases) - len(failures), "total": len(cases), "failures": failures}
