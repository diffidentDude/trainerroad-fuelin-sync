"""Read-only TrainerRoad calendar preview. No Fuelin writes or intensity guesses."""
import argparse
import json
import math
import os
import re
import sys
from datetime import date, datetime, time, timedelta, timezone as utc_timezone
from pathlib import Path
from urllib.request import urlopen
from zoneinfo import ZoneInfo
from config import load_config, DEFAULT_START_TIMES


def unescape(value):
    return re.sub(r"\\([nN,;\\])", lambda m: "\n" if m[1].lower() == "n" else m[1], value)


def events(text):
    # RFC 5545 line unfolding must precede property and text parsing.
    lines = re.sub(r"\r?\n[ \t]", "", text.strip("\r\n")).splitlines()
    event = None
    for line in lines:
        if line == "BEGIN:VEVENT":
            event = {}
        elif line == "END:VEVENT":
            if event is not None:
                yield event
            event = None
        elif event is not None and ":" in line:
            key, value = line.split(":", 1)
            parts = key.split(";")
            name = parts[0].upper()
            params = dict(p.split("=", 1) for p in parts[1:] if "=" in p)
            if name in event:
                raise ValueError(f"Repeated {name} property requires a full calendar parser")
            event[name] = (params, value)


def start(event, timezone):
    params, value = event["DTSTART"]
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        return date.fromisoformat(value), None
    parsed = datetime.strptime(value.rstrip("Z"), "%Y%m%dT%H%M%S")
    tz = ZoneInfo("UTC" if value.endswith("Z") else params.get("TZID", timezone).strip('"'))
    local = parsed.replace(tzinfo=tz).astimezone(ZoneInfo(timezone))
    return local.date(), local.isoformat()


def scheduled_start(day, title, timestamp, timezone, config=None):
    """Fill missing times using personal rules; retain explicit calendar times."""
    if timestamp is not None:
        return timestamp, "calendar"
    rules = config or {}
    title_rule = rules.get("titles", {}).get(title, {})
    is_gym = title_rule.get("type") in ("gym", "strength", "weights") or re.search(
        r"\b(gym|strength|weights)\b", title, re.I
    )
    rule = "gym" if is_gym else "weekend" if day.weekday() >= 5 else "weekday"
    times = {**DEFAULT_START_TIMES, **rules.get("start_times", {})}
    clock = time.fromisoformat(times[rule])
    return datetime.combine(day, clock, ZoneInfo(timezone)).isoformat(), rule


def calendar_duration(event, timezone):
    """Read structured ICS duration; an all-day span is not workout duration."""
    if "DURATION" in event:
        raw = event["DURATION"][1]
        match = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", raw)
        if not match or not any(match.groups()):
            raise ValueError("Unsupported ICS duration")
        days, hours, minutes, seconds = (int(value or 0) for value in match.groups())
        if not (days and not (hours or minutes or seconds) and start(event, timezone)[1] is None):
            return days * 1440 + hours * 60 + minutes + seconds / 60, "ics_duration"
    if "DTEND" in event:
        begin = start(event, timezone)[1]
        end = start({"DTSTART": event["DTEND"]}, timezone)[1]
        if begin and end:
            delta = datetime.fromisoformat(end).astimezone(utc_timezone.utc) - datetime.fromisoformat(begin).astimezone(utc_timezone.utc)
            return delta.total_seconds() / 60, "ics_end"
    return None, None


def preview(text, today, timezone, duration_overrides=None, future_only=False, config=None):
    monday = today if future_only else today - timedelta(days=today.weekday())
    end = None if future_only else monday + timedelta(days=14)
    workouts, skipped, seen = [], [], set()
    for event in events(text):
        if any(key in event for key in ("RRULE", "RDATE", "RECURRENCE-ID", "EXDATE")):
            raise ValueError("Recurring events require recurrence support; preview stopped to avoid omissions")
        if event.get("STATUS", ({}, ""))[1] == "CANCELLED":
            continue
        if "DTSTART" not in event:
            raise ValueError("Calendar event is missing DTSTART")
        day, timestamp = start(event, timezone)
        if day < monday or (end is not None and day >= end):
            continue
        title = unescape(event.get("SUMMARY", ({}, ""))[1])
        match = re.fullmatch(r"(\d+):([0-5]\d)\s+-\s+(.+)", title)
        workout_title = match[3] if match else title
        if match:
            duration, duration_source = int(match[1]) * 60 + int(match[2]), "title"
        else:
            duration, duration_source = calendar_duration(event, timezone)
            if duration is None:
                duration, duration_source = (duration_overrides or {}).get(title), "configured_override"
        if duration is None:
            skipped.append({"date": str(day), "title": title, "reason": "No recognised duration prefix; may be an annotation"})
            continue
        if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
            raise ValueError("Duration must be positive and finite")
        uid = event.get("UID", ({}, ""))[1]
        if not uid or uid in seen:
            raise ValueError("Workout UID missing or duplicated; stable identity cannot be established")
        seen.add(uid)
        timestamp, time_source = scheduled_start(day, workout_title, timestamp, timezone, config)
        description = unescape(event.get("DESCRIPTION", ({}, ""))[1])
        metrics = {}
        for name, pattern in {
            "tss": r"\bTSS\s+(\d+(?:\.\d+)?)",
            "intensity_factor": r"\bIF\s+(\d+(?:\.\d+)?)",
            "energy_kj": r"\bkJ(?:\(Cal\))?\s+(\d+(?:\.\d+)?)",
        }.items():
            found = re.search(pattern, description, re.I)
            metrics[name] = float(found[1]) if found else None
        workouts.append({
            "source_uid": uid, "title": workout_title, "date": str(day),
            "start_time": timestamp, "start_time_source": time_source,
            "duration_minutes": duration, "duration_source": duration_source,
            "activity_type": None, "fuelin_intensity": None,
            "needs_review": ["Confirm activity type", "Verify mapping to Fuelin intensity 1–5"],
            "description": description, **metrics,
        })
    return {"mode": "preview_only", "window_start": str(monday), "window_end_exclusive": str(end) if end is not None else None,
            "timezone": timezone, "workouts": sorted(workouts, key=lambda w: (w["date"], w["start_time"] or "", w["source_uid"])),
            "skipped_events": skipped}


def read_calendar(file=None):
    """Fetch a complete calendar without exposing its private URL in logs."""
    if file:
        raw = Path(file).read_bytes()
    else:
        url = os.environ.get("TRAINERROAD_CALENDAR_URL", "").replace("webcal://", "https://", 1)
        if not url.startswith("https://"):
            raise ValueError("Set TRAINERROAD_CALENDAR_URL or use --file")
        with urlopen(url, timeout=30) as response:
            raw = response.read(10_000_001)
    if len(raw) > 10_000_000:
        raise ValueError("Calendar exceeds size limit")
    text = raw.decode("utf-8-sig")
    lines = re.sub(r"\r?\n[ \t]", "", text.strip("\r\n")).splitlines()
    if not lines or lines[0] != "BEGIN:VCALENDAR" or lines[-1] != "END:VCALENDAR":
        raise ValueError("Input is not a complete ICS calendar")
    in_event = False
    for line in lines:
        if line == "BEGIN:VEVENT":
            if in_event:
                raise ValueError("Nested or truncated event")
            in_event = True
        elif line == "END:VEVENT":
            if not in_event:
                raise ValueError("Unmatched event boundary")
            in_event = False
    if in_event:
        raise ValueError("Unclosed event")
    return text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, help="Local ICS export; otherwise use TRAINERROAD_CALENDAR_URL")
    parser.add_argument("--timezone", help="Override the configured timezone")
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--today", type=date.fromisoformat, help="Override local date for reproducible previews")
    args = parser.parse_args()
    try:
        rules = load_config(args.config)
        timezone = args.timezone or rules.get("timezone", "UTC")
        zone = ZoneInfo(timezone)
        text = read_calendar(args.file)
        overrides = {title: rule["duration_minutes"] for title, rule in rules.get("titles", {}).items() if "duration_minutes" in rule}
        result = preview(text, args.today or datetime.now(zone).date(), timezone, overrides, config=rules)
        print(json.dumps(result, indent=2))
    except Exception:
        # Network exception strings may contain the private subscription URL.
        print("Preview failed. Check the calendar, timezone and connection. No activities were written.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
