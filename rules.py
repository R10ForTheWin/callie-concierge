"""Meeting Window Finder: business rules (deterministic, standard library only).

Decides which meeting windows to offer. No randomness, network, or AI calls.
Every decision returns {result, rules_fired, explanation, windows}.
"""
import re
from datetime import date, timedelta

RULES_VERSION = "1.3"

MEETING_TYPES = ["Zoom", "Coffee", "Lunch", "Drinks"]
TIME_ZONES = {"PT": 0, "MT": 60, "CT": 120, "ET": 180}  # minutes ahead of Pacific

# R3: hold length in minutes
HOLD_MINUTES = {"Zoom": 60, "Coffee": 90, "Lunch": 120, "Drinks": 120}
# R7: allowed start times in Pacific, minutes after midnight (first, last)
START_RANGE = {
    "Zoom": (9 * 60, 16 * 60 + 30),
    "Coffee": (8 * 60, 15 * 60),
    "Lunch": (11 * 60 + 30, 13 * 60),
    "Drinks": (17 * 60, 19 * 60),
}
START_STEP = 30
# R4: drive time each way by zone, plus a fixed pad each way
DRIVE_MINUTES = {"Local": 15, "Nearby": 30, "Mid": 45, "Valley": 60}
PAD_MINUTES = 15
# Zone map: area keyword (lowercase) -> zone
ZONE_MAP = {
    "torrance": "Local", "redondo": "Local", "hermosa": "Local",
    "manhattan beach": "Local", "el segundo": "Local", "palos verdes": "Local",
    "south bay": "Local",
    "lax": "Nearby", "westchester": "Nearby", "culver city": "Nearby", "long beach": "Nearby",
    "westside": "Mid", "santa monica": "Mid", "mid-city": "Mid", "downtown": "Mid",
    "hollywood": "Mid",
    "valley": "Valley", "pasadena": "Valley", "burbank": "Valley", "sherman oaks": "Valley",
    "encino": "Valley", "studio city": "Valley",
}
# R5: allowed weekdays (Monday = 0)
ZOOM_DAYS = {0, 1, 2, 3, 4}
IN_PERSON_DAYS = {1, 2, 3}
# R8: Zoom start must fall inside these hours in the contact's time zone
CONTACT_FIRST, CONTACT_LAST = 8 * 60, 17 * 60
LOOK_AHEAD_DAYS = 14
# R18: unless you turn it off, the first window is at least this many working days out
LEAD_BUSINESS_DAYS = 5
MAX_WINDOWS = 4
MIN_WINDOWS = 3

DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
BUSY_RE = re.compile(r"^([0-9]{4}-[0-9]{2}-[0-9]{2}) ([0-9]{2}):([0-9]{2})-([0-9]{2}):([0-9]{2})(?: (.*))?$")

RULE_TEXT = {
    "R1": "A field is missing or invalid",
    "R2": "The area is not on your zone map and no zone was picked",
    "R3": "Hold length set by meeting type",
    "R4": "Drive time each way by zone, plus a 15-minute pad",
    "R5": "Allowed days for this meeting type",
    "R6": "No windows on travel days",
    "R7": "Start times allowed for this meeting type",
    "R8": "Zoom starts between 8:00 and 17:00 in the contact's time zone",
    "R9": "No drinks in a week that already has drinks",
    "R10": "The whole hold, drive and pad included, must not overlap your calendar",
    "R11": "Fewer than 3 windows found",
    "R12": "Offer the earliest open slot on each of the first 4 qualifying days",
    "R13": "No reply after 2 business days: send a nudge",
    "R14": "No reply after 3 business days: release the holds",
    "R15": "No reply after 7 calendar days: you decide",
    "R16": "They replied: no follow-up",
    "R17": "Otherwise: wait",
    "R18": "Look busy: windows start at least 5 working days out",
}

FOLLOW_UP_RESULTS = ["You decide", "Release holds", "Send a nudge", "Wait", "No follow-up"]
NUDGE_BUSINESS_DAYS = 2
RELEASE_BUSINESS_DAYS = 3
DECIDE_CALENDAR_DAYS = 7


def _parse_date(text):
    if not isinstance(text, str) or not DATE_RE.match(text):
        return None
    try:
        return date(int(text[0:4]), int(text[5:7]), int(text[8:10]))
    except ValueError:
        return None


def _clock(h, m):
    h, m = int(h), int(m)
    if h > 24 or m > 59 or (h == 24 and m != 0):
        return None
    return h * 60 + m


def validate(raw):
    """Read every field once. Returns (typed_values, None) or (None, error)."""
    if not isinstance(raw, dict):
        return None, "input is not a set of fields"
    mtype = raw.get("meeting_type")
    if not isinstance(mtype, str) or mtype.strip() not in MEETING_TYPES:
        return None, "meeting_type must be one of " + ", ".join(MEETING_TYPES)
    mtype = mtype.strip()

    tz = raw.get("contact_tz")
    if not isinstance(tz, str) or tz.strip() not in TIME_ZONES:
        return None, "contact_tz must be one of " + ", ".join(TIME_ZONES)
    tz = tz.strip()

    area = raw.get("area", "")
    if area is None:
        area = ""
    if not isinstance(area, str):
        return None, "area must be text"
    area = area.strip()
    if mtype != "Zoom" and area == "":
        return None, "area is required for an in-person meeting"

    today = _parse_date((raw.get("request_date") or "").strip() if isinstance(raw.get("request_date"), str) else None)
    if today is None:
        return None, "request_date must be a real date written YYYY-MM-DD"

    busy_text = raw.get("busy", "")
    if busy_text is None:
        busy_text = ""
    if not isinstance(busy_text, str):
        return None, "busy must be text, one block per line"
    busy = []
    for line in busy_text.splitlines():
        line = line.strip()
        if line == "":
            continue
        m = BUSY_RE.match(line)
        if not m:
            return None, "busy line not understood: " + line
        d = _parse_date(m.group(1))
        start = _clock(m.group(2), m.group(3))
        end = _clock(m.group(4), m.group(5))
        if d is None or start is None or end is None or end <= start:
            return None, "busy line has an invalid date or time: " + line
        label = (m.group(6) or "").strip()
        busy.append((d, start, end, label))

    travel_text = raw.get("travel_days", "")
    if travel_text is None:
        travel_text = ""
    if not isinstance(travel_text, str):
        return None, "travel_days must be text"
    travel = set()
    for part in travel_text.split(","):
        part = part.strip()
        if part == "":
            continue
        d = _parse_date(part)
        if d is None:
            return None, "travel_days must be dates written YYYY-MM-DD, separated by commas"
        travel.add(d)

    zone = raw.get("zone", "")
    if zone is None:
        zone = ""
    if not isinstance(zone, str) or (zone.strip() and zone.strip() not in DRIVE_MINUTES):
        return None, "zone must be one of " + ", ".join(DRIVE_MINUTES) + " or blank"
    zone = zone.strip()

    look_busy = raw.get("look_busy", True)
    if not isinstance(look_busy, bool):
        return None, "look_busy must be true or false"

    return {
        "look_busy": look_busy,
        "meeting_type": mtype, "area": area, "contact_tz": tz, "zone": zone,
        "request_date": today, "busy": busy, "travel_days": travel,
    }, None


def zone_for(area):
    a = area.lower()
    for key in sorted(ZONE_MAP, key=len, reverse=True):
        if key in a:
            return ZONE_MAP[key]
    return None


def _fmt(minutes):
    h, m = divmod(minutes, 60)
    suffix = "AM" if h < 12 or h == 24 else "PM"
    h12 = h % 12 or 12
    return "%d:%02d %s" % (h12, m, suffix)


def find_windows(raw):
    typed, error = validate(raw)
    if error:
        return {
            "result": "You decide", "rules_fired": ["R1"], "windows": [],
            "explanation": "R1 fired: " + error + ". Nothing was offered.",
        }

    mtype = typed["meeting_type"]
    in_person = mtype != "Zoom"
    zone = None
    if in_person:
        zone = zone_for(typed["area"]) or typed["zone"] or None
        if zone is None:
            return {
                "result": "You decide", "rules_fired": ["R2"], "windows": [],
                "explanation": "R2 fired: '" + typed["area"] + "' is not on your zone map, so the drive time is unknown.",
            }

    fired = {"R3", "R7"}
    notes = ["R3: %s holds %d min." % (mtype, HOLD_MINUTES[mtype])]
    buffer = 0
    if in_person:
        buffer = DRIVE_MINUTES[zone] + PAD_MINUTES
        fired.add("R4")
        notes.append("R4: %s zone, %d min drive + %d pad each way." % (zone, DRIVE_MINUTES[zone], PAD_MINUTES))

    allowed_days = IN_PERSON_DAYS if in_person else ZOOM_DAYS
    offset = TIME_ZONES[typed["contact_tz"]]
    first, last = START_RANGE[mtype]
    hold = HOLD_MINUTES[mtype]
    drinks_weeks = set()
    for d, _, _, label in typed["busy"]:
        if "drinks" in label.lower():
            drinks_weeks.add(d.isocalendar()[:2])

    first_day = typed["request_date"] + timedelta(days=1)
    if typed["look_busy"]:
        first_day = add_business_days(typed["request_date"], LEAD_BUSINESS_DAYS)
        fired.add("R18")
        notes.append("R18: nothing before %s %s." % (first_day.strftime("%a %b"), first_day.day))

    # every day from tomorrow to the end of the search, with why it was or wasn't used
    days = []
    day = typed["request_date"] + timedelta(days=1)
    while day < first_day:
        days.append({"date": day.isoformat(), "status": "too_soon"})
        day += timedelta(days=1)

    windows = []
    for i in range(LOOK_AHEAD_DAYS):
        day = first_day + timedelta(days=i)
        if len(windows) >= MAX_WINDOWS:
            days.append({"date": day.isoformat(), "status": "not_needed"})
            continue
        if day.weekday() not in allowed_days:
            fired.add("R5")
            days.append({"date": day.isoformat(), "status": "wrong_day"})
            continue
        if day in typed["travel_days"]:
            fired.add("R6")
            days.append({"date": day.isoformat(), "status": "travel"})
            continue
        if mtype == "Drinks" and day.isocalendar()[:2] in drinks_weeks:
            fired.add("R9")
            days.append({"date": day.isoformat(), "status": "drinks_week"})
            continue
        day_busy = [(s, e) for d, s, e, _ in typed["busy"] if d == day]
        found = False
        for start in range(first, last + 1, START_STEP):
            if mtype == "Zoom":
                local = start + offset
                if local < CONTACT_FIRST or local > CONTACT_LAST:
                    fired.add("R8")
                    continue
            block_start, block_end = start - buffer, start + hold + buffer
            if any(block_start < e and s < block_end for s, e in day_busy):
                fired.add("R10")
                continue
            windows.append({
                "date": day.isoformat(),
                "start_pt": _fmt(start),
                "start_contact": _fmt(start + offset) + " " + typed["contact_tz"],
                "hold_from": _fmt(block_start),
                "hold_to": _fmt(block_end),
            })
            found = True
            break
        days.append({"date": day.isoformat(), "status": "window" if found else "full"})

    if len(windows) < MIN_WINDOWS:
        fired.add("R11")
        result = "You decide"
        notes.append("R11: only %d window(s) found in %d days." % (len(windows), LOOK_AHEAD_DAYS))
    else:
        fired.add("R12")
        result = "Offer windows"
        notes.append("R12: offering %d windows." % len(windows))

    order = sorted(fired, key=lambda r: int(r[1:]))
    return {"result": result, "rules_fired": order, "windows": windows, "explanation": " ".join(notes), "days": days}


def add_business_days(start, n):
    """The nth weekday after start."""
    day = start
    while n > 0:
        day += timedelta(days=1)
        if day.weekday() < 5:
            n -= 1
    return day


def last_window_day(request_date, look_busy=True):
    """Furthest day find_windows can look at, so callers know how much calendar to load."""
    first = add_business_days(request_date, LEAD_BUSINESS_DAYS) if look_busy else request_date + timedelta(days=1)
    return first + timedelta(days=LOOK_AHEAD_DAYS - 1)


def business_days_between(start, end):
    """Weekdays after start, up to and including end."""
    count, day = 0, start
    while day < end:
        day += timedelta(days=1)
        if day.weekday() < 5:
            count += 1
    return count


def follow_up(raw):
    """Decide what to do about windows you sent. Returns {result, rules_fired, explanation}."""
    if not isinstance(raw, dict):
        return {"result": "You decide", "rules_fired": ["R1"], "explanation": "R1 fired: input is not a set of fields."}
    sent = _parse_date(raw.get("sent_date").strip()) if isinstance(raw.get("sent_date"), str) else None
    today = _parse_date(raw.get("today").strip()) if isinstance(raw.get("today"), str) else None
    replied = raw.get("replied").strip() if isinstance(raw.get("replied"), str) else None
    if sent is None or today is None or replied not in ("Yes", "No") or today < sent:
        return {"result": "You decide", "rules_fired": ["R1"],
                "explanation": "R1 fired: sent date, today, and replied (Yes or No) must be valid, and today cannot be before the sent date."}
    if replied == "Yes":
        return {"result": "No follow-up", "rules_fired": ["R16"],
                "explanation": "R16 fired: they replied, so mark the chosen window and delete the other holds."}
    biz = business_days_between(sent, today)
    cal = (today - sent).days
    fired, result = [], "Wait"
    if biz >= NUDGE_BUSINESS_DAYS:
        fired.append("R13"); result = "Send a nudge"
    if biz >= RELEASE_BUSINESS_DAYS:
        fired.append("R14"); result = "Release holds"
    if cal >= DECIDE_CALENDAR_DAYS:
        fired.append("R15"); result = "You decide"
    if not fired:
        fired.append("R17")
    return {"result": result, "rules_fired": fired,
            "explanation": "%d business day(s), %d calendar day(s) since you sent the windows, no reply. %s" % (biz, cal, result + ".")}


BASE_BUSY = "\n".join([
    "2026-10-20 09:00-12:00 Class",
    "2026-10-21 13:00-15:00 Team meeting",
    "2026-10-22 18:00-21:00 Class",
    "2026-10-27 09:00-12:00 Class",
    "2026-10-29 18:00-21:00 Class",
])

SCENARIOS = [
    {"id": "T1", "name": "Typical Zoom",
     "input": {"meeting_type": "Zoom", "area": "", "contact_tz": "PT", "request_date": "2026-10-19", "look_busy": False,
               "busy": BASE_BUSY, "travel_days": ""},
     "expected": "Offer windows", "rules": ["R3", "R7", "R10", "R12"]},
    {"id": "T2", "name": "Coffee in Torrance",
     "input": {"meeting_type": "Coffee", "area": "Torrance", "contact_tz": "PT", "request_date": "2026-10-19", "look_busy": False,
               "busy": BASE_BUSY, "travel_days": ""},
     "expected": "Offer windows", "rules": ["R3", "R4", "R5", "R7", "R10", "R12"]},
    {"id": "T3", "name": "Zoom with New York, at the 5 PM ET edge",
     "input": {"meeting_type": "Zoom", "area": "", "contact_tz": "ET", "request_date": "2026-10-19", "look_busy": False,
               "busy": "2026-10-20 09:00-14:00 Offsite", "travel_days": ""},
     "expected": "Offer windows", "rules": ["R3", "R7", "R10", "R12"]},
    {"id": "T4", "name": "Drinks in a week that already has drinks",
     "input": {"meeting_type": "Drinks", "area": "Santa Monica", "contact_tz": "PT", "request_date": "2026-10-19", "look_busy": False,
               "busy": BASE_BUSY + "\n2026-10-21 18:00-20:00 Drinks with Sam", "travel_days": ""},
     "expected": "You decide", "rules": ["R3", "R4", "R5", "R7", "R9", "R10", "R11"]},
    {"id": "T5", "name": "Worst case: Valley lunch on a travel-heavy, booked fortnight",
     "input": {"meeting_type": "Lunch", "area": "Pasadena", "contact_tz": "PT", "request_date": "2026-10-19", "look_busy": False,
               "busy": BASE_BUSY, "travel_days": "2026-10-22, 2026-10-28"},
     "expected": "You decide", "rules": ["R3", "R4", "R5", "R6", "R7", "R10", "R11"]},
    {"id": "T6", "name": "Area not on the zone map",
     "input": {"meeting_type": "Lunch", "area": "Ojai", "contact_tz": "PT", "request_date": "2026-10-19", "look_busy": False,
               "busy": BASE_BUSY, "travel_days": ""},
     "expected": "You decide", "rules": ["R2"]},
    {"id": "T7", "name": "Meeting type missing",
     "input": {"meeting_type": "", "area": "Torrance", "contact_tz": "PT", "request_date": "2026-10-19", "look_busy": False,
               "busy": BASE_BUSY, "travel_days": ""},
     "expected": "You decide", "rules": ["R1"]},
    {"id": "T8", "name": "My place with a picked zone",
     "input": {"meeting_type": "Drinks", "area": "All American Brewery", "zone": "Valley", "contact_tz": "PT",
               "request_date": "2026-10-19", "look_busy": False, "busy": BASE_BUSY, "travel_days": ""},
     "expected": "Offer windows", "rules": ["R3", "R4", "R5", "R7", "R10", "R12"]},
    {"id": "M1", "name": "Malformed date",
     "input": {"meeting_type": "Zoom", "area": "", "contact_tz": "PT", "request_date": "2026-13-40", "look_busy": False,
               "busy": BASE_BUSY, "travel_days": ""},
     "expected": "You decide", "rules": ["R1"]},
    {"id": "M2", "name": "Malformed busy time",
     "input": {"meeting_type": "Zoom", "area": "", "contact_tz": "PT", "request_date": "2026-10-19", "look_busy": False,
               "busy": "2026-10-20 25:00-26:00 Class", "travel_days": ""},
     "expected": "You decide", "rules": ["R1"]},
    {"id": "M4", "name": "Unknown zone name",
     "input": {"meeting_type": "Coffee", "area": "Ojai", "zone": "Far", "contact_tz": "PT", "request_date": "2026-10-19", "look_busy": False,
               "busy": BASE_BUSY, "travel_days": ""},
     "expected": "You decide", "rules": ["R1"]},
    {"id": "M3", "name": "Malformed travel day",
     "input": {"meeting_type": "Coffee", "area": "Torrance", "contact_tz": "PT", "request_date": "2026-10-19", "look_busy": False,
               "busy": BASE_BUSY, "travel_days": "next Tue"},
     "expected": "You decide", "rules": ["R1"]},
    {"id": "T9", "name": "Look busy: Zoom starts 5 working days out",
     "input": {"meeting_type": "Zoom", "area": "", "contact_tz": "PT", "request_date": "2026-10-19",
               "busy": BASE_BUSY, "travel_days": ""},
     "expected": "Offer windows", "rules": ["R3", "R7", "R10", "R12", "R18"]},
    {"id": "T10", "name": "Look busy over a weekend: Friday request, nothing before next Friday",
     "input": {"meeting_type": "Coffee", "area": "Torrance", "contact_tz": "PT", "request_date": "2026-10-23",
               "busy": BASE_BUSY, "travel_days": "", "look_busy": True},
     "expected": "Offer windows", "rules": ["R3", "R4", "R5", "R7", "R12", "R18"]},
    {"id": "M5", "name": "Look busy not true or false",
     "input": {"meeting_type": "Zoom", "area": "", "contact_tz": "PT", "request_date": "2026-10-19",
               "busy": BASE_BUSY, "travel_days": "", "look_busy": "yes"},
     "expected": "You decide", "rules": ["R1"]},
]


FOLLOW_UP_SCENARIOS = [
    {"id": "F1", "name": "Sent yesterday, no reply",
     "input": {"sent_date": "2026-10-19", "today": "2026-10-20", "replied": "No"},
     "expected": "Wait", "rules": ["R17"]},
    {"id": "F2", "name": "Two business days, no reply",
     "input": {"sent_date": "2026-10-19", "today": "2026-10-21", "replied": "No"},
     "expected": "Send a nudge", "rules": ["R13"]},
    {"id": "F3", "name": "Three business days, no reply",
     "input": {"sent_date": "2026-10-19", "today": "2026-10-22", "replied": "No"},
     "expected": "Release holds", "rules": ["R13", "R14"]},
    {"id": "F4", "name": "A week, no reply",
     "input": {"sent_date": "2026-10-19", "today": "2026-10-26", "replied": "No"},
     "expected": "You decide", "rules": ["R13", "R14", "R15"]},
    {"id": "F5", "name": "Sent Friday, checked Monday",
     "input": {"sent_date": "2026-10-23", "today": "2026-10-26", "replied": "No"},
     "expected": "Wait", "rules": ["R17"]},
    {"id": "F6", "name": "They replied",
     "input": {"sent_date": "2026-10-19", "today": "2026-10-26", "replied": "Yes"},
     "expected": "No follow-up", "rules": ["R16"]},
    {"id": "F7", "name": "Today before sent date",
     "input": {"sent_date": "2026-10-19", "today": "2026-10-18", "replied": "No"},
     "expected": "You decide", "rules": ["R1"]},
]


def self_test():
    passed, failures = 0, []
    cases = [(s, find_windows) for s in SCENARIOS] + [(s, follow_up) for s in FOLLOW_UP_SCENARIOS]
    for s, fn in cases:
        out = fn(s["input"])
        if out["result"] == s["expected"] and out["rules_fired"] == s["rules"]:
            passed += 1
        else:
            failures.append({"id": s["id"], "expected": [s["expected"], s["rules"]],
                             "actual": [out["result"], out["rules_fired"]]})
    return {"passed": passed, "total": len(cases), "failures": failures}


def determinism_check(inputs, runs=25):
    seen = set()
    for _ in range(runs):
        out = find_windows(inputs)
        seen.add(repr((out["result"], out["rules_fired"], out["windows"])))
    return len(seen)
