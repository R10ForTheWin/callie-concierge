# Callie

Your personal Calendar Concierge. Paste a meeting request, and Callie finds 3–4 windows that fit your rules, drafts the reply, gives you holds for Apple Calendar, and tells you when to nudge someone who hasn't answered.

Callie never sends anything. You copy the reply, you import the holds.

## How it decides
`rules.py` makes every scheduling decision. Claude only reads the request and drafts the words, and code checks everything Claude writes before you see it.

- **Holds:** Zoom 60 min · Coffee 90 · Lunch 120 · Drinks 120
- **Drive each way (from the South Bay), plus a 15-min pad:** Local 15 · Nearby 30 · Mid 45 · Valley 60
  - Local: Torrance, Redondo, Hermosa, Manhattan Beach, El Segundo, Palos Verdes
  - Nearby: LAX, Westchester, Culver City, Long Beach
  - Mid: Westside, Santa Monica, Mid-City, Downtown, Hollywood
  - Valley: SFV, Pasadena, Burbank, Sherman Oaks, Encino, Studio City
  - Places not on the map: pick a zone once and Callie remembers it ("My places", on your device)
- **Days:** Zoom Mon–Fri; in person Tue–Thu; never on travel days
- **Start times (PT):** Zoom 9–4:30 · Coffee 8–3 · Lunch 11:30–1 · Drinks 5–7; Zoom must also fall 8–5 in their time zone
- **Drinks:** at most one per week
- **No overlaps:** the whole hold, drive and pad included, must clear your calendar
- **Look busy:** the first window is at least 5 working days out. A switch turns this off for one request; it's back on for the next
- **3–4 windows** within 14 days of the first allowed day, earliest open slot per day; fewer than 3 → you decide
- **Follow-ups:** 2 business days → nudge · 3 → release holds · 7 days → you decide

## Using it
Add Callie to your iPhone home screen (Share → Add to Home Screen) and it opens full screen like an app.
- **New:** paste the request, check the details, find windows. Tap a window to leave it out; the reply, holds, and follow-up use only the ones you keep. Below the windows, **Your calendar around them** shows what's on each day and why Callie skipped the others.
- **Waiting:** everyone you've sent times to, with when to nudge or release holds.
- **Calendar:** your Apple Calendar sync, typed holds, and everything Callie plans around.

## Apple Calendar (read only)
Callie reads your iCloud calendar so you don't paste busy blocks. It never writes to it.
- Timed events become busy blocks (shown in Pacific time). Events marked Free, or cancelled, are skipped. A title with "drinks" counts toward the one-drinks-a-week rule.
- All-day events count as travel days only when the title says travel, trip, flight, vacation, OOO, out of town, or away. Birthdays and holidays are ignored.
- It syncs when the page opens (cached 5 minutes) or when you tap **Sync now**. You can still add blocks by hand; Callie uses both.

## Type a hold
Under My calendar, type or dictate something like "hold Thursday 2–4 for coffee". Claude reads the day and time; code checks that the date is real and within 4 weeks, the times make sense, and the weekday matches what you typed. You confirm it, and you can also download it for Apple Calendar. With only a start time, the hold lasts as long as the meeting type it names (coffee 90, lunch 120…) or an hour.

Check the rules any time: Settings → Check my rules (runs all tests in `rules.py` and `applecal.py`).

## Run locally
```
pip install -r requirements.txt
ANTHROPIC_API_KEY=... python app.py   # http://localhost:5050
```

## Deploy (Railway)
GitHub `R10ForTheWin/callie` → Railway auto-detects the `Procfile`.

Environment variables:
- `ANTHROPIC_API_KEY` (required for reading requests and drafting replies)
- `CALLIE_PASSCODE` (recommended; the page asks for it once per device)
- `CALLIE_MODEL` (optional; defaults to `claude-opus-5-5`)
- `ICLOUD_USER` + `ICLOUD_APP_PASSWORD` (optional; connects Apple Calendar. Use your Apple ID email and an app-specific password from account.apple.com → Sign-In and Security → App-Specific Passwords. Requires `CALLIE_PASSCODE`.)
- `ICLOUD_CALENDARS` (optional; comma-separated calendar names to read. Default: all)

Your busy blocks, travel days, places, and follow-up list are saved in the browser on each device, not on the server.

## Ideas for later
- Text Callie from your phone
- Waze departure times · Outlook

`spec/` (not in git) holds the original AI Studio build files; `ai-studio-export/` is the old Gemini version.
