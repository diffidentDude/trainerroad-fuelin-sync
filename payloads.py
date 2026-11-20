"""Build local Fuelin request previews. Does not make network requests."""
import argparse
import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from config import load_config, DEFAULT_IF_RANGES, validate_if_ranges


def payload(workout, settings, allowed_types, zone=None):
    activity = settings.get("type", workout.get("activity_type"))
    intensity = settings.get("intensity", workout.get("fuelin_intensity"))
    if activity not in allowed_types:
        raise ValueError("Choose a supported Fuelin activity type")
    if type(intensity) is not int or not 1 <= intensity <= 5:
        raise ValueError("Choose a Fuelin intensity from 1 to 5")
    starts = datetime.fromisoformat(workout["start_time"])
    if starts.utcoffset() is None:
        raise ValueError("Start time must include a timezone offset")
    duration = workout["duration_minutes"]
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
        raise ValueError("Duration must be positive and finite")
    local_zone = zone or starts.tzinfo
    ends = (starts.astimezone(timezone.utc) + timedelta(minutes=duration)).astimezone(local_zone)
    starts = starts.astimezone(local_zone)
    return {"type": activity, "intensity": intensity,
            "startsAt": starts.replace(tzinfo=None).isoformat(),
            "endsAt": ends.replace(tzinfo=None).isoformat()}


def intensity_from_if(value, ranges=None):
    """Ranges include max; shared endpoints belong to the lower level. Zero is included."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("IF must be a finite non-negative number")
    bands = validate_if_ranges(DEFAULT_IF_RANGES if ranges is None else ranges)
    for band in bands:
        lower_matches = value >= band["min"] if band["level"] == 1 else value > band["min"]
        if lower_matches and (band["max"] is None or value <= band["max"]):
            return band["level"]
    raise ValueError("IF ranges do not cover the workout")


def estimate_if(tss, duration_minutes):
    """Invert power-based cycling TSS = hours * IF² * 100."""
    if type(tss) not in (int, float) or not math.isfinite(tss) or tss < 0:
        raise ValueError("TSS must be finite and non-negative")
    if type(duration_minutes) not in (int, float) or not math.isfinite(duration_minutes) or duration_minutes <= 0:
        raise ValueError("Duration must be finite and positive")
    result = math.sqrt(tss / (duration_minutes / 60) / 100)
    if not math.isfinite(result):
        raise ValueError("Estimated IF is not finite")
    return result


def classify(workout, rules):
    """Translate TR IF using the personal scale; retain gym and no-IF race defaults."""
    title = workout["title"]
    summary = re.split(r"\bGoals\s*:", workout.get("description", ""), maxsplit=1, flags=re.I)[0]
    declared_type = workout.get("activity_type")
    if declared_type in ("gym", "strength", "weights") or re.search(r"\b(gym|strength|weights)\b", title, re.I):
        return {"type": "gym", "intensity": rules.get("gym", 1)}, "gym"
    cycling = declared_type in ("bike", "ride") or (declared_type is None and re.search(r"\b(FTP|cycling|riding|bike|ride)\b", title + " " + summary, re.I))
    settings = {"type": declared_type} if declared_type else {"type": "bike"} if cycling else {}
    if workout.get("intensity_factor") is not None:
        try:
            return {**settings, "intensity": intensity_from_if(workout["intensity_factor"], rules.get("if_ranges"))}, "intensity_factor"
        except ValueError:
            return settings, "invalid_intensity_factor"
    if cycling and workout.get("tss") is not None:
        try:
            estimated = estimate_if(workout["tss"], workout.get("duration_minutes"))
            return {**settings, "intensity": intensity_from_if(estimated, rules.get("if_ranges")),
                    "estimated_if": estimated}, "tss_duration"
        except (ValueError, OverflowError, ZeroDivisionError):
            return settings, "invalid_tss_or_duration"
    if re.search(r"\b(race|racing|criterium|crit)\b", title, re.I):
        return {**settings, "intensity": rules.get("race", 5)}, "race_default"
    return settings, "missing_intensity_factor"


def build_plan(preview, config, schema):
    allowed = schema["components"]["schemas"]["InsertWorkout"]["properties"]["type"]["enum"]
    entries = []
    for workout in preview["workouts"]:
        # Title rules cover repeating workouts; UID overrides handle individual sessions.
        title_rule = config.get("titles", {}).get(workout["title"], {})
        workout_rule = config.get("workouts", {}).get(workout["source_uid"], {})
        declared_type = workout_rule.get("type", title_rule.get("type", workout.get("activity_type")))
        inferred, classification = classify({**workout, "activity_type": declared_type}, config.get("rpe", {}))
        settings = {**inferred,
                    **{key: workout[source] for key, source in [("type", "activity_type"), ("intensity", "fuelin_intensity")]
                       if workout.get(source) is not None},
                    **config.get("titles", {}).get(workout["title"], {}),
                    **config.get("workouts", {}).get(workout["source_uid"], {})}
        entry = {"source_uid": workout["source_uid"], "title": workout["title"], "date": workout["date"],
                 "classification": classification}
        if "estimated_if" in inferred:
            entry["estimated_if"] = inferred["estimated_if"]
        try:
            entry["body"] = payload(workout, settings, allowed, ZoneInfo(preview["timezone"]))
            entry["status"] = "ready_for_review"
        except (ValueError, TypeError, KeyError) as error:
            entry.update(status="needs_review", reason=str(error))
        entries.append(entry)
    return {"mode": "payload_preview_only", "timezone": preview["timezone"],
            "endpoint_template": "/api/v1/users/{userId}/days/{date}/workouts",
            "entries": entries}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preview", type=Path, default=Path("preview.json"))
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--schema", type=Path, default=Path("fuelin-schema.json"))
    args = parser.parse_args()
    plan = build_plan(json.loads(args.preview.read_text()), load_config(args.config),
                      json.loads(args.schema.read_text()))
    print(json.dumps(plan, indent=2))


if __name__ == "__main__":
    main()
