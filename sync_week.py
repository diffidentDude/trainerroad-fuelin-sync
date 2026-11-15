"""Sync all future workouts exposed by the TrainerRoad calendar feed."""
import argparse
import json
import os
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from zoneinfo import ZoneInfo
from uuid import UUID

from auth_store import StorageError
from create_workout import FuelinClient, SyncError, activity_family, comparison_details, conflicting_workout, create_workout, same_workout, workout_instant
from payloads import build_plan
from config import load_config, ConfigError
from preview import preview, read_calendar


def week_start(selection, today):
    monday = today - timedelta(days=today.weekday())
    if selection == "next":
        return monday + timedelta(days=7)
    if selection == "current":
        return monday
    result = date.fromisoformat(selection)
    if result.weekday() != 0:
        raise SyncError("--week must be current, next, or a Monday YYYY-MM-DD")
    return result


def available_dates(client, first, last, page_size=100):
    """Read materialised Fuelin days; never invent a nutrition-planning horizon."""
    found, seen = set(), set()
    offset = 0
    while True:
        query = urlencode({"from": first, "to": last, "view": "calendar", "limit": page_size, "offset": offset})
        page = client.request("GET", f"/api/v1/users/{client.user_id}/days?{query}")
        if not isinstance(page, list):
            raise SyncError("Unexpected Fuelin day-list response; no create requests sent")
        if not page:
            return found
        for item in page:
            value = item.get("date") if isinstance(item, dict) else None
            if not isinstance(value, str):
                raise SyncError("Fuelin day list is missing dates; no create requests sent")
            try:
                day = date.fromisoformat(value[:10]).isoformat()
            except ValueError:
                raise SyncError("Fuelin day list contains an invalid date; no create requests sent") from None
            if day in seen:
                raise SyncError("Fuelin day pagination repeated dates; no create requests sent")
            seen.add(day)
            if first <= day <= last:
                found.add(day)
        # Continue until empty; servers may cap the requested page size.
        offset += len(page)


def plan_week(client, entries, zone, now):
    """Read all relevant days before planning any writes."""
    days = sorted({entry["date"] for entry in entries})
    existing = {}
    for day in days:
        try:
            values = client.request("GET", f"/api/v1/users/{client.user_id}/days/{day}/workouts")
        except HTTPError as error:
            code = error.code
            error.close()
            if code != 404:
                raise SyncError(f"Fuelin workout read failed for {day} (HTTP {code}); no create requests sent") from None
            # Distinguish a missing parent day from a failed workout route.
            query = urlencode({"from": day, "to": day, "view": "calendar", "limit": 2})
            try:
                day_list = client.request("GET", f"/api/v1/users/{client.user_id}/days?{query}")
            except HTTPError as day_error:
                day_code = day_error.code
                day_error.close()
                raise SyncError(f"Fuelin workout read returned 404 for {day}; day lookup returned HTTP {day_code}. No create requests sent") from None
            if isinstance(day_list, list) and not day_list:
                raise SyncError(f"Fuelin has no day record for {day}: workouts returned 404 and the date-filtered day list was empty. No create requests sent; no nutrition days were fabricated")
            raise SyncError(f"Fuelin workout read returned 404 for {day}, but the day lookup was not empty. No create requests sent; response handling needs investigation")
        if not isinstance(values, list) or any(not isinstance(value, dict) for value in values):
            raise SyncError("Unexpected Fuelin workout list; no sync writes attempted")
        existing[day] = values
    result, claimed = [], set()
    for entry in entries:
        action, reason, candidate = "review", "", None
        body = entry.get("body")
        if entry["status"] != "ready_for_review":
            reason = entry.get("reason", "Unresolved workout")
        elif workout_instant(body["startsAt"], zone) <= now:
            action, reason = "past", "Already started; left unchanged"
        else:
            values = existing[entry["date"]]
            ambiguous_family = False
            matches = [value for value in values if same_workout(value, body, zone)]
            if not matches:
                matches = [value for value in values if conflicting_workout(value, body, zone)]
            if not matches:
                # A unique same-time session can be matched when the sport changes.
                clock_matches = []
                for value in values:
                    try:
                        if all(workout_instant(value[key], zone) == workout_instant(body[key], zone) for key in ("startsAt", "endsAt")):
                            clock_matches.append(value)
                    except (KeyError, TypeError, ValueError, AttributeError):
                        pass
                clock_sources = [source for source in entries if source["date"] == entry["date"] and source.get("body") and all(source["body"][key] == body[key] for key in ("startsAt", "endsAt"))]
                if len(clock_sources) == 1:
                    matches = clock_matches
            if not matches:
                family_sources = [source for source in entries if source["date"] == entry["date"] and source.get("body") and activity_family(source["body"]["type"]) == activity_family(body["type"])]
                # A single session of this sport on both sides can be matched even
                # when its start time changed enough that the intervals don't overlap.
                family_values = [value for value in values if activity_family(value.get("type")) == activity_family(body["type"])]
                if len(family_sources) == 1:
                    matches = family_values
                elif family_values:
                    ambiguous_family = True
            if len(matches) == 1:
                candidate = matches[0]
                if same_workout(candidate, body, zone):
                    action, reason = "matched", "Existing activity already matches"
                else:
                    action, reason = "update", "Update the existing activity to match TrainerRoad"
            elif matches or ambiguous_family:
                reason = "Multiple source or existing activities could match; requires review"
            else:
                action, reason = "create", "Missing from Fuelin"
        if candidate is not None:
            identity = candidate.get("id")
            if not identity or identity in claimed:
                action, reason = "review", "Existing activity is missing an ID or matches another source"
            else:
                claimed.add(identity)
            if action == "update" and (candidate.get("isCompleted") is True or candidate.get("recurringActivityId") is not None):
                action, reason = "review", "Completed or recurring Fuelin activity requires review before updating"
        details = []
        if action in ("review", "update") and body:
            candidates = [candidate] if candidate is not None else [value for value in existing[entry["date"]] if conflicting_workout(value, body, zone)]
            for index, value in enumerate(candidates, 1):
                details.append(f"Existing candidate {index}:")
                details.extend("  " + line for line in comparison_details(value, body, zone))
        item = {"entry": entry, "action": action, "reason": reason, "details": details,
                "snapshot": existing[entry["date"]]}
        if candidate is not None and candidate.get("id"):
            item["fuelin_id"] = candidate["id"]
        result.append(item)
    # Two source sessions may collide even when neither exists remotely yet.
    for index, item in enumerate(result):
        if item["action"] not in ("create", "update"):
            continue
        for other in result[index + 1:]:
            if other["action"] in ("create", "update") and other["entry"]["date"] == item["entry"]["date"] and conflicting_workout(other["entry"]["body"], item["entry"]["body"], zone):
                item["action"] = other["action"] = "review"
                item["reason"] = other["reason"] = "Source sessions overlap; check start-time rules"
    return result


def execute_week(client, actions, zone):
    if any(item["action"] == "review" for item in actions):
        raise SyncError("Resolve the review items first. No sync writes attempted")
    for item in actions:
        if item["action"] == "update":
            try:
                UUID(item["fuelin_id"])
            except (KeyError, TypeError, ValueError, AttributeError):
                raise SyncError("A matching Fuelin activity has no valid ID; no sync writes attempted") from None
    counts = {"submitted": 0, "updated": 0, "matched": 0}
    for item in actions:
        entry = item["entry"]
        if item["action"] == "matched":
            counts["matched"] += 1
        elif item["action"] == "update":
            if workout_instant(entry["body"]["startsAt"], zone) <= datetime.now(zone):
                raise SyncError("A workout has started since planning; stopped without updating it")
            uid = str(UUID(item["fuelin_id"]))
            route = f"/api/v1/users/{client.user_id}/days/{entry['date']}/workouts/{uid}"
            response = client.request("PATCH", route, {**entry["body"], "workoutId": uid})
            if isinstance(response, dict) and (response.get("success") is False or (type(response.get("status")) in (int, float) and response["status"] >= 400)):
                raise SyncError("Fuelin reported an update error")
            counts["updated"] += 1
            print(f"Update submitted: {entry['date']} {entry['title']}")
        elif item["action"] == "create":
            if workout_instant(entry["body"]["startsAt"], zone) <= datetime.now(zone):
                raise SyncError("A workout has started since planning; stopped without creating it")
            create_workout(client, entry, zone, existing=item["snapshot"])
            counts["submitted"] += 1
            print(f"Submitted: {entry['date']} {entry['title']}")
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--week", help="Optional restriction: next, current, or Monday YYYY-MM-DD; default is all future workouts")
    parser.add_argument("--file", type=Path, help="Use an explicit ICS file instead of fetching the calendar URL")
    parser.add_argument("--timezone", help="Override the configured timezone")
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--apply", action="store_true", help="Create missing sessions and update matching activities to TrainerRoad values")
    parser.add_argument("--keep-manual-gym", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.keep_manual_gym:
        print("--keep-manual-gym is obsolete: existing activities now follow TrainerRoad values.")
    try:
        rules = load_config(args.config)
        args.timezone = args.timezone or rules.get("timezone", "UTC")
        zone = ZoneInfo(args.timezone)
        now = datetime.now(zone)
        monday = week_start(args.week, now.date()) if args.week else now.date()
        if not args.file and not os.environ.get("TRAINERROAD_CALENDAR_URL"):
            raise SyncError("TRAINERROAD_CALENDAR_URL is not set in this terminal. Export it as described in README.md, or use --file with an ICS export")
        overrides = {title: rule["duration_minutes"] for title, rule in rules.get("titles", {}).items() if "duration_minutes" in rule}
        source = preview(read_calendar(args.file), monday, args.timezone, overrides, future_only=not bool(args.week), config=rules)
        sunday = monday + timedelta(days=6) if args.week else None
        if sunday is not None:
            source["workouts"] = [workout for workout in source["workouts"] if str(monday) <= workout["date"] <= str(sunday)]
        skipped = [event for event in source["skipped_events"] if sunday is None or str(monday) <= event["date"] <= str(sunday)]
        annotation_titles = {title.casefold().strip() for title in rules.get("annotations", [])}
        annotations = [event for event in skipped if event["title"].casefold().strip() in annotation_titles]
        skipped = [event for event in skipped if event["title"].casefold().strip() not in annotation_titles]
        plan = build_plan(source, rules, json.loads(Path("fuelin-schema.json").read_text()))
        scope = f"Week: {monday} to {sunday}" if sunday else f"All planned workouts from {monday} in the calendar feed"
        print(f"{scope} ({args.timezone}); {len(plan['entries'])} source workouts")
        if annotations:
            print(f"Ignored {len(annotations)} recognised training-phase annotations (not workouts).")
        for workout in source["workouts"]:
            if workout.get("duration_source") == "configured_override":
                print(f"Duration override: {workout['date']} {workout['title']} = {workout['duration_minutes']} minutes (no title prefix)")
        if not plan["entries"]:
            print("No future source workouts in the selected range. No Fuelin activities changed.")
            return 0
        client = FuelinClient()
        user = client.login()
        if user.get("timezone") and user["timezone"] != args.timezone:
            raise SyncError("Fuelin timezone differs from the calendar timezone")
        target_dates = sorted({entry["date"] for entry in plan["entries"]})
        open_dates = available_dates(client, target_dates[0], target_dates[-1])
        deferred = sum(entry["date"] not in open_dates for entry in plan["entries"])
        plan["entries"] = [entry for entry in plan["entries"] if entry["date"] in open_dates]
        skipped = [event for event in skipped if event["date"] in open_dates]
        if open_dates:
            print(f"Fuelin available dates: {min(open_dates)} to {max(open_dates)}; {len(plan['entries'])} eligible workouts, {deferred} deferred until their days open.")
        else:
            print(f"Fuelin has no available dates in this range; {deferred} workouts deferred. No create requests sent.")
            return 0
        if skipped:
            print(f"{len(skipped)} available-date calendar entries have no recognised duration.")
            for event in skipped:
                print(f"SKIPPED {event['date']} {event['title']!r}: {event['reason']}")
        if not plan["entries"]:
            print("No workouts are eligible yet. Run again when Fuelin opens their dates.")
            return 0
        print(f"Reading Fuelin state for {len(set(entry['date'] for entry in plan['entries']))} training dates before sending any requests...")
        actions = plan_week(client, plan["entries"], zone, now)
        for item in actions:
            entry = item["entry"]
            body = entry.get("body", {})
            starts = workout_instant(body["startsAt"], zone).strftime("%H:%M") if body else "?"
            print(f"{item['action'].upper():7} {entry['date']} {starts} {entry['title']} (intensity {body.get('intensity', '?')}): {item['reason']}")
            for line in item.get("details", []):
                print("  " + line)
        print("Summary: " + ", ".join(f"{count} {action}" for action, count in sorted(Counter(item["action"] for item in actions).items())))
        if not args.apply:
            print("Preview only: no Fuelin activities changed. Use --apply to create missing workouts.")
            return 1 if any(item["action"] == "review" for item in actions) else 0
        if skipped:
            raise SyncError("Unrecognised calendar entries need review; no sync writes attempted")
        counts = execute_week(client, actions, zone)
        print(f"Sync run: {counts['submitted']} create requests submitted, {counts['updated']} update requests submitted, {counts['matched']} already matching. No post-write reads or retries.")
        return 0
    except (StorageError, SyncError, ConfigError) as error:
        print(str(error))
    except HTTPError as error:
        print(f"Fuelin request returned HTTP {error.code}; stopped without retrying writes.")
    except Exception:
        print("Sync stopped. Check the calendar URL/file, connection and configuration. Private response details were not printed.")
    print("No automatic write retries. A failed request may still have reached Fuelin; check the app before rerunning.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
