"""Callie: your personal Calendar Concierge.

Flask backend. rules.py makes every scheduling decision; Claude only reads the
request email and drafts the words, and deterministic checks run on everything
Claude returns before the page shows it.
"""
import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import anthropic
from flask import Flask, Response, jsonify, render_template, request

import applecal
import rules

app = Flask(__name__)

MODEL = os.environ.get("CALLIE_MODEL", "claude-opus-5-5")
PASSCODE = os.environ.get("CALLIE_PASSCODE", "")
PACIFIC = ZoneInfo("America/Los_Angeles")
FALLBACK_BETA = "server-side-fallback-2026-07-01"

_client = None


def client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(timeout=60.0, max_retries=1)
    return _client


# ---------- Claude calls ----------

class AIError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


def ask_claude(system, prompt, schema, max_tokens=2000):
    """One Claude call that must return JSON matching schema."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise AIError("Claude key missing. Set ANTHROPIC_API_KEY on Railway.", 500)
    try:
        resp = client().beta.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
            betas=[FALLBACK_BETA],
            fallbacks="default",
        )
    except anthropic.AuthenticationError:
        raise AIError("Claude key missing or invalid. Set ANTHROPIC_API_KEY on Railway.", 500)
    except anthropic.RateLimitError:
        raise AIError("Claude is busy right now. Your rules still work; try again in a minute.", 429)
    except anthropic.APITimeoutError:
        raise AIError("Claude took too long. Your rules still work; try again.", 504)
    except anthropic.APIStatusError as e:
        raise AIError("Claude error %s. Your rules still work." % e.status_code, 502)
    except anthropic.APIConnectionError:
        raise AIError("Couldn't reach Claude. Your rules still work.", 502)
    if resp.stop_reason == "refusal":
        raise AIError("Claude declined this request. Enter the fields by hand.", 422)
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except ValueError:
        raise AIError("Claude's answer wasn't readable. Try again.", 502)


def require_passcode():
    if PASSCODE and request.headers.get("X-Callie-Passcode", "") != PASSCODE:
        return jsonify({"error": "Enter your Callie passcode in Settings.", "passcode": True}), 401
    return None


# ---------- deterministic checks on AI output ----------

FIELD_LABELS = {"meeting_type": "Meeting type", "area": "Area", "contact_tz": "Contact time zone"}
COMMIT_WORDS = ["confirmed", "booked", "scheduled", "invite", "invited", "approve", "approved"]
TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\s?(?:AM|PM)\b", re.I)


def check_read(fields, missing, text):
    """E1, E2 and validation for AI-proposed fields. Returns {field: problem}."""
    problems = {}
    lowered = text.lower()
    mtype = fields.get("meeting_type") or ""
    if mtype not in rules.MEETING_TYPES:
        problems["meeting_type"] = "Missing"
    tz = fields.get("contact_tz") or ""
    if tz not in rules.TIME_ZONES:
        problems["contact_tz"] = "Missing"
    area = (fields.get("area") or "").strip()
    if mtype != "Zoom":
        if not area:
            problems["area"] = "Missing"
        elif area.lower() not in lowered:
            problems["area"] = "Not in the request"  # E1
    for name in missing or []:  # E2: anything the AI flagged stays blank
        if name in FIELD_LABELS and name not in problems and not (name == "area" and mtype == "Zoom"):
            problems[name] = "Missing"
    return problems


def pretty_date(iso):
    d = datetime.strptime(iso, "%Y-%m-%d")
    return d.strftime("%a %b ") + str(d.day)


def check_draft(summary, windows, max_words):
    """D1-D4. Returns a list of {id, name, ok, reason}."""
    out = []
    missing = [w for w in windows
               if w["start_contact"].lower() not in summary.lower() or pretty_date(w["date"]).lower() not in summary.lower()]
    out.append({"id": "D1", "name": "Every window named", "ok": not missing,
                "reason": "" if not missing else "Missing " + ", ".join(pretty_date(w["date"]) + " " + w["start_contact"] for w in missing)})
    allowed = {w["start_contact"].rsplit(" ", 1)[0].replace(" ", "").upper() for w in windows}
    extra = [t for t in TIME_RE.findall(summary) if t.replace(" ", "").upper() not in allowed]
    out.append({"id": "D2", "name": "No other times", "ok": not extra, "reason": "" if not extra else "Unexpected " + ", ".join(extra)})
    words = [w for w in COMMIT_WORDS if re.search(r"\b" + w + r"\b", summary, re.I)]
    out.append({"id": "D3", "name": "No commitment words", "ok": not words, "reason": "" if not words else "Uses " + ", ".join(words)})
    count = len(summary.split())
    out.append({"id": "D4", "name": "%d words or fewer" % max_words, "ok": count <= max_words, "reason": "" if count <= max_words else "%d words" % count})
    return out


def check_question(question, failing):
    synonyms = {"meeting_type": ["zoom", "coffee", "lunch", "drinks", "meeting type"],
                "area": ["where", "area", "location", "place"],
                "contact_tz": ["time zone", "timezone"]}
    q = question.lower()
    ok1 = all(any(s in q for s in synonyms[f]) for f in failing)
    ok2 = not any(re.search(r"\b" + w + r"\b", question, re.I) for w in COMMIT_WORDS)
    ok3 = len(question.split()) <= 40
    return [{"id": "Q1", "name": "Asks for every missing field", "ok": ok1},
            {"id": "Q2", "name": "No commitment words", "ok": ok2},
            {"id": "Q3", "name": "40 words or fewer", "ok": ok3}]


# ---------- prompts ----------

READ_SYSTEM = (
    "You read one meeting request email and propose values for three fields: meeting_type (one of Zoom, Coffee, "
    "Lunch, Drinks), area (the place or neighborhood named for an in-person meeting, copied exactly as written; empty "
    "for Zoom), and contact_tz (one of PT, MT, CT, ET; only if the email names a time zone or a city; an in-person "
    "meeting in Los Angeles is PT). Copy places exactly as written. Never guess a missing value: leave it empty and "
    "list the field name in missing. You propose; the business rules check your values and a person confirms them."
)
READ_SCHEMA = {
    "type": "object",
    "properties": {
        "meeting_type": {"type": "string", "enum": ["Zoom", "Coffee", "Lunch", "Drinks", ""]},
        "area": {"type": "string"},
        "contact_tz": {"type": "string", "enum": ["PT", "MT", "CT", "ET", ""]},
        "confidence": {"type": "integer"},
        "missing": {"type": "array", "items": {"type": "string", "enum": ["meeting_type", "area", "contact_tz"]}},
    },
    "required": ["meeting_type", "area", "contact_tz", "confidence", "missing"],
    "additionalProperties": False,
}
QUESTION_SYSTEM = (
    "You write one short, polite question to the person who asked to meet, asking for the details their request is "
    "missing. Name each missing detail plainly. Do not guess values, do not propose times, and never say a meeting is "
    "confirmed, booked, or scheduled. 40 words or fewer."
)
QUESTION_SCHEMA = {"type": "object", "properties": {"question": {"type": "string"}},
                   "required": ["question"], "additionalProperties": False}
DRAFT_SYSTEM = (
    "You draft a short, warm email reply for DJ, a busy professional in Los Angeles, offering the meeting windows you "
    "are given. Your only job is the wording. List every window you are given, in the contact's time zone exactly as "
    "given, one per line, each written like \"Tue Oct 20, 5:00 PM ET\". Use no other times. Tone: friendly, confident, "
    "and brief; interested but not eager. Never use these words: confirmed, booked, scheduled, invite, invited, "
    "approve, approved. Never add, remove, or change a window, and never invent facts, names, or places. "
    "Sign off as DJ."
)
DRAFT_SCHEMA = {"type": "object",
                "properties": {"summary": {"type": "string"}, "needs_review": {"type": "boolean"}},
                "required": ["summary", "needs_review"], "additionalProperties": False}

HOLD_SYSTEM = (
    "You turn one short note DJ typed or dictated, like \"hold Thursday 2-4 for coffee\", into a calendar hold. "
    "Pick the date from the list of upcoming days you are given; a bare weekday means its next date on the list, "
    "\"next <weekday>\" means the one in the following week. Times are 24-hour HH:MM in Pacific time. A time without "
    "AM or PM from 1 to 7 means PM; 8 to 11 means AM; 12 means noon. Leave end empty if the note gives only a start. "
    "label is a few words saying what the hold is for, from the note; use Hold if it doesn't say. Never guess a "
    "missing date or start: leave it empty and list it in missing."
)
HOLD_SCHEMA = {
    "type": "object",
    "properties": {
        "date": {"type": "string"}, "start": {"type": "string"}, "end": {"type": "string"}, "label": {"type": "string"},
        "missing": {"type": "array", "items": {"type": "string", "enum": ["date", "start"]}},
    },
    "required": ["date", "start", "end", "label", "missing"],
    "additionalProperties": False,
}
HOLD_DAYS = 28
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
HHMM_RE = re.compile(r"^([01][0-9]|2[0-3]):([0-5][0-9])$")


def hold_days(today):
    return [today + timedelta(days=i) for i in range(HOLD_DAYS + 1)]


def default_end(start, label):
    """No end given: use the meeting type's hold length if the label names one, else an hour."""
    h, m = map(int, start.split(":"))
    length = next((mins for t, mins in rules.HOLD_MINUTES.items() if t.lower() in label.lower()), 60)
    end = min(h * 60 + m + length, 24 * 60 - 1)
    return "%02d:%02d" % divmod(end, 60)


def check_hold(got, text, today):
    """H1-H4 on an AI-proposed hold. Returns (hold or None, checks)."""
    days = {d.isoformat(): d for d in hold_days(today)}
    date_s, start, end = got.get("date") or "", got.get("start") or "", got.get("end") or ""
    label = re.sub(r"\s+", " ", got.get("label") or "").strip()[:60] or "Hold"
    ok_date = date_s in days
    ok_time = bool(HHMM_RE.match(start)) and (end == "" or bool(HHMM_RE.match(end)))
    if ok_time and not end:
        end = default_end(start, label)
    ok_order = ok_time and end > start
    named = [i for i, w in enumerate(WEEKDAYS) if re.search(r"\b" + w[:3] + r"(?:" + w[3:] + r")?\b", text, re.I)]
    ok_day = not ok_date or not named or days[date_s].weekday() in named
    checks = [{"id": "H1", "name": "Date in the next %d days" % HOLD_DAYS, "ok": ok_date},
              {"id": "H2", "name": "Real start and end times", "ok": ok_time},
              {"id": "H3", "name": "Ends after it starts", "ok": ok_order},
              {"id": "H4", "name": "Weekday matches your note", "ok": ok_day}]
    if not all(c["ok"] for c in checks):
        return None, checks
    return {"date": date_s, "start": start, "end": end, "label": label}, checks


def keep_only(windows, keep):
    """The windows you left selected on the page, matched against what the rules decided. None keeps all."""
    if keep is None:
        return windows
    keep = set(keep)
    return [w for w in windows if w["date"] + " " + w["start_pt"] in keep]


def window_lines(windows):
    return "\n".join("- " + pretty_date(w["date"]) + ", " + w["start_contact"] for w in windows)


# ---------- routes ----------

@app.get("/")
def index():
    return render_template("index.html", rules_version=rules.RULES_VERSION, passcode_required=bool(PASSCODE),
                           calendar_connected=applecal.configured())


@app.post("/api/windows")
def api_windows():
    return jsonify(rules.find_windows(request.get_json(silent=True) or {}))


@app.post("/api/followup")
def api_followup():
    return jsonify(rules.follow_up(request.get_json(silent=True) or {}))


@app.get("/api/selftest")
def api_selftest():
    st = rules.self_test()
    base = rules.SCENARIOS[1]["input"]
    st["determinism"] = {"runs": 25, "distinct": rules.determinism_check(base, 25)}
    try:
        cal = applecal.self_test()
    except ImportError:
        cal = {"passed": 0, "total": 0, "failures": []}
    st["passed"] += cal["passed"]
    st["total"] += cal["total"]
    st["failures"] += cal["failures"]
    st["calendar"] = {"connected": applecal.configured()}
    return jsonify(st)


@app.get("/api/calendar")
def api_calendar():
    if not applecal.configured():
        return jsonify({"error": "Apple Calendar isn't connected. Set ICLOUD_USER and ICLOUD_APP_PASSWORD on Railway."}), 404
    if not PASSCODE:  # your calendar is private: never serve it without a passcode
        return jsonify({"error": "Set CALLIE_PASSCODE on Railway before connecting your calendar."}), 403
    blocked = require_passcode()
    if blocked:
        return blocked
    today = datetime.now(PACIFIC).date()
    try:
        days = (rules.last_window_day(today) - today).days
        data = applecal.sync(today, days, refresh=request.args.get("refresh") == "1")
    except applecal.CalendarError as e:
        return jsonify({"error": str(e)}), 502
    return jsonify(data)


@app.get("/api/rules")
def api_rules():
    scenarios = [{"id": s["id"], "name": s["name"], "input": s["input"], "expected": s["expected"],
                  "rules": s["rules"], "kind": "windows"} for s in rules.SCENARIOS]
    scenarios += [{"id": s["id"], "name": s["name"], "input": s["input"], "expected": s["expected"],
                   "rules": s["rules"], "kind": "followup"} for s in rules.FOLLOW_UP_SCENARIOS]
    zones = {}
    for key, zone in rules.ZONE_MAP.items():
        zones.setdefault(zone, []).append(key.title())
    return jsonify({"version": rules.RULES_VERSION, "rules": rules.RULE_TEXT, "scenarios": scenarios,
                    "zones": zones, "drive": rules.DRIVE_MINUTES, "pad": rules.PAD_MINUTES,
                    "hold": rules.HOLD_MINUTES})


@app.post("/api/ai/read")
def api_read():
    blocked = require_passcode()
    if blocked:
        return blocked
    text = ((request.get_json(silent=True) or {}).get("text") or "").strip()
    if not text:
        return jsonify({"error": "Paste a request first."}), 400
    log = []
    try:
        prompt = "Meeting request email:\n<email>\n" + text + "\n</email>"
        got = ask_claude(READ_SYSTEM, prompt, READ_SCHEMA)
        problems = check_read(got, got.get("missing"), text)
        if problems:
            log.append("retried")
            retry = prompt + "\n\nYour last proposal failed these checks: " + json.dumps(problems) + \
                ". Re-read the email. Leave a field empty if the email doesn't say it."
            got = ask_claude(READ_SYSTEM, retry, READ_SCHEMA)
            problems = check_read(got, got.get("missing"), text)
        fields = {k: (got.get(k) or "") for k in FIELD_LABELS}
        for k in problems:  # E2: failing fields stay blank, never guessed
            fields[k] = ""
        if fields["meeting_type"] == "Zoom":
            fields["area"] = ""
        result = {"fields": fields, "confidence": max(1, min(10, int(got.get("confidence") or 1))),
                  "problems": {k: v for k, v in problems.items()}, "log": log}
        if problems:
            failing = list(problems)
            labels = [FIELD_LABELS[f] for f in failing]
            qprompt = "Request:\n<email>\n" + text + "\n</email>\nMissing details: " + ", ".join(labels)
            q = ask_claude(QUESTION_SYSTEM, qprompt, QUESTION_SCHEMA, 800)
            checks = check_question(q.get("question", ""), failing)
            if not all(c["ok"] for c in checks):
                q = ask_claude(QUESTION_SYSTEM, qprompt + "\nYour last question failed: " +
                               ", ".join(c["name"] for c in checks if not c["ok"]), QUESTION_SCHEMA, 800)
                checks = check_question(q.get("question", ""), failing)
            result["question"] = {"text": q.get("question", ""), "checks": checks,
                                  "ok": all(c["ok"] for c in checks)}
            log.append("asked the requester" if result["question"]["ok"] else "sent to a person")
        else:
            log.append("passed first time" if not log else "passed after one retry")
        return jsonify(result)
    except AIError as e:
        return jsonify({"error": str(e)}), e.status


def draft_with_checks(prompt, windows, max_words):
    got = ask_claude(DRAFT_SYSTEM, prompt, DRAFT_SCHEMA, 1500)
    checks = check_draft(got.get("summary", ""), windows, max_words)
    retried = False
    if not all(c["ok"] for c in checks):
        retried = True
        fail = "; ".join(c["id"] + " " + c["name"] + (": " + c["reason"] if c["reason"] else "") for c in checks if not c["ok"])
        got = ask_claude(DRAFT_SYSTEM, prompt + "\n\nYour last draft failed these checks: " + fail + ". Fix them.", DRAFT_SCHEMA, 1500)
        checks = check_draft(got.get("summary", ""), windows, max_words)
    ok = all(c["ok"] for c in checks)
    return {"summary": got.get("summary", ""), "needs_review": bool(got.get("needs_review")) or not ok,
            "checks": checks, "ok": ok,
            "log": ("passed after one retry" if retried else "passed first time") if ok else "held for a person"}


@app.post("/api/ai/draft")
def api_draft():
    blocked = require_passcode()
    if blocked:
        return blocked
    body = request.get_json(silent=True) or {}
    decided = rules.find_windows(body.get("fields") or {})  # re-decide on the server; never trust the page
    if decided["result"] != "Offer windows":
        return jsonify({"error": "No windows to offer. Your rules sent this one to you."}), 400
    windows = keep_only(decided["windows"], body.get("keep"))
    if not windows:
        return jsonify({"error": "Pick at least one window to offer."}), 400
    f = body["fields"]
    where = "over Zoom" if f.get("meeting_type") == "Zoom" else "for " + f.get("meeting_type", "").lower() + " in " + f.get("area", "")
    prompt = ("Write DJ's reply offering to meet " + where + ". The contact's time zone is " + f.get("contact_tz", "") +
              ". The windows:\n" + window_lines(windows) +
              ("\n\nTheir email, for context only:\n<email>\n" + body["request_text"] + "\n</email>" if body.get("request_text") else "") +
              "\nReply in 90 words or fewer.")
    try:
        return jsonify(draft_with_checks(prompt, windows, 90))
    except AIError as e:
        return jsonify({"error": str(e)}), e.status


@app.post("/api/ai/nudge")
def api_nudge():
    blocked = require_passcode()
    if blocked:
        return blocked
    body = request.get_json(silent=True) or {}
    decided = rules.find_windows(body.get("fields") or {})
    today = body.get("today") or ""
    windows = [w for w in keep_only(decided.get("windows", []), body.get("keep")) if w["date"] > today]
    if not windows:
        return jsonify({"error": "Every window you sent has passed. Find fresh windows instead."}), 400
    prompt = ("Write a two-sentence friendly follow-up from DJ to someone who hasn't replied about meeting. Re-offer "
              "these windows, in their time zone:\n" + window_lines(windows) + "\nKeep it to 60 words or fewer.")
    try:
        return jsonify(draft_with_checks(prompt, windows, 60))
    except AIError as e:
        return jsonify({"error": str(e)}), e.status


@app.post("/api/ai/hold")
def api_hold():
    blocked = require_passcode()
    if blocked:
        return blocked
    text = ((request.get_json(silent=True) or {}).get("text") or "").strip()
    if not text:
        return jsonify({"error": "Type a hold first, like: hold Thursday 2-4 for coffee."}), 400
    today = datetime.now(PACIFIC).date()
    days = "\n".join(d.strftime("%a ") + d.isoformat() + (" (today)" if d == today else "") for d in hold_days(today))
    prompt = "Upcoming days:\n" + days + "\n\nDJ's note:\n<note>\n" + text[:300] + "\n</note>"
    try:
        got = ask_claude(HOLD_SYSTEM, prompt, HOLD_SCHEMA, 600)
        if got.get("missing"):
            return jsonify({"error": "Callie needs the " + " and ".join("day" if m == "date" else "start time" for m in got["missing"]) +
                                     ". Try: hold Thursday 2-4 for coffee."}), 422
        hold, checks = check_hold(got, text, today)
        if hold is None:
            fail = "; ".join(c["id"] + " " + c["name"] for c in checks if not c["ok"])
            got = ask_claude(HOLD_SYSTEM, prompt + "\n\nYour last answer failed these checks: " + fail + ". Fix them.", HOLD_SCHEMA, 600)
            hold, checks = check_hold(got, text, today)
        if hold is None:
            return jsonify({"error": "Callie couldn't pin that down. Add it with the date and time pickers below.", "checks": checks}), 422
    except AIError as e:
        return jsonify({"error": str(e)}), e.status
    hold["line"] = "%s %s-%s %s" % (hold["date"], hold["start"], hold["end"], hold["label"])
    return jsonify({"hold": hold, "checks": checks})


def ics_escape(s):
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def to_utc(date_iso, label):
    """'2026-10-20' + '12:30 PM' (Pacific) -> UTC iCalendar stamp."""
    local = datetime.strptime(date_iso + " " + label, "%Y-%m-%d %I:%M %p").replace(tzinfo=PACIFIC)
    return local.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


@app.post("/api/ics")
def api_ics():
    body = request.get_json(silent=True) or {}
    f = body.get("fields") or {}
    windows = keep_only(rules.find_windows(f)["windows"], body.get("keep"))
    if not windows:
        return jsonify({"error": "No windows to hold."}), 400
    title = "HOLD: " + f.get("meeting_type", "") + (" – " + f.get("area") if f.get("area") else " – Zoom")
    if body.get("who"):
        title += " (" + body["who"] + ")"
    seed = hashlib.sha1(json.dumps(f, sort_keys=True).encode()).hexdigest()[:10]
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Callie//Calendar Concierge//EN", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH"]
    for w in windows:
        lines += ["BEGIN:VEVENT", "UID:callie-%s-%s-%s@callie" % (seed, w["date"], w["start_pt"].replace(":", "").replace(" ", "")),
                  "DTSTAMP:" + now, "DTSTART:" + to_utc(w["date"], w["hold_from"]),
                  "DTEND:" + to_utc(w["date"], w["hold_to"]), "SUMMARY:" + ics_escape(title),
                  "DESCRIPTION:" + ics_escape("Offered by Callie. Meeting starts " + w["start_pt"] + " PT" +
                                              ("" if w["start_contact"].endswith(" PT") else " (" + w["start_contact"] + ")") +
                                              ". Hold includes drive time and pad. Delete if not chosen."),
                  "TRANSP:OPAQUE", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return Response("\r\n".join(lines) + "\r\n", mimetype="text/calendar",
                    headers={"Content-Disposition": "attachment; filename=callie-holds.ics"})


@app.post("/api/ics/block")
def api_ics_block():
    b = request.get_json(silent=True) or {}
    line = "%s %s-%s %s" % (b.get("date", ""), b.get("start", ""), b.get("end", ""), b.get("label") or "Hold")
    typed, _ = rules.validate({"meeting_type": "Zoom", "contact_tz": "PT", "request_date": "2000-01-01", "busy": line})
    if typed is None or not typed["busy"]:
        return jsonify({"error": "That hold isn't a real date and time."}), 400
    d, start, end, label = typed["busy"][0]
    day = datetime(d.year, d.month, d.day, tzinfo=PACIFIC)
    stamp = lambda mins: (day + timedelta(minutes=mins)).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    uid = hashlib.sha1(line.encode()).hexdigest()[:12]
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Callie//Calendar Concierge//EN", "CALSCALE:GREGORIAN",
             "METHOD:PUBLISH", "BEGIN:VEVENT", "UID:callie-block-%s@callie" % uid,
             "DTSTAMP:" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
             "DTSTART:" + stamp(start), "DTEND:" + stamp(end), "SUMMARY:" + ics_escape("HOLD: " + label),
             "DESCRIPTION:" + ics_escape("Added by typing in Callie."), "TRANSP:OPAQUE", "END:VEVENT", "END:VCALENDAR"]
    return Response("\r\n".join(lines) + "\r\n", mimetype="text/calendar",
                    headers={"Content-Disposition": "attachment; filename=callie-hold.ics"})


@app.get("/healthz")
def healthz():
    return "ok"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5050)), debug=True)
