"""Preview or create one selected Fuelin workout. Run with --apply to write."""
import argparse
import getpass
import http.cookiejar
import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, time, timezone
from pathlib import Path
from urllib.parse import quote
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener
from uuid import UUID
from zoneinfo import ZoneInfo

from check_login import NoRedirect, session_user_id, token_user_id
from payloads import build_plan
from config import load_config, ConfigError
from auth_store import KeychainStore, StorageError


class SyncError(ValueError):
    """An application-owned message safe to show to the user."""


class FuelinClient:
    def __init__(self):
        self.opener = build_opener(NoRedirect(), HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.token = None
        self.user_id = None

    def request(self, method, path, body=None):
        headers = {"Accept": "application/json", "User-Agent": "TrainerRoad-Fuelin-Personal-Sync/0.1"}
        if method == "GET":
            headers["Cache-Control"] = "no-cache"
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request("https://prod.fuelin.io/core" + path, method=method, headers=headers,
                          data=json.dumps(body).encode() if body is not None else None)
        with self.opener.open(request, timeout=30) as response:
            return json.loads(response.read(2_000_000))

    def login(self, refresh=False, store=None):
        store = store if store is not None else KeychainStore()
        saved = store.load()
        email = saved["email"] if saved else None
        if saved and not refresh:
            self.token, self.user_id = saved["token"], str(UUID(saved["user_id"]))
            try:
                user = self._profile(email)
                print("Using saved Fuelin session from macOS Keychain.")
                return user
            except HTTPError as error:
                if error.code != 401:
                    raise
                error.close()
                store.delete()
                print("Saved Fuelin session expired; please sign in again.")
        self.token, self.user_id = None, None
        email = email or input("Fuelin email: ").strip()
        result = self.request("POST", "/api/v1/auth/signin", {"email": email, "password": getpass.getpass("Fuelin password (hidden): ")})
        token = result.get("token") if isinstance(result, dict) else None
        if not isinstance(token, str) or not token:
            raise SyncError("Login did not return a token")
        self.token = token
        uid = token_user_id(token) or session_user_id(self.opener, token, email)
        if not uid:
            raise SyncError("Could not resolve session identity")
        self.user_id = str(UUID(uid))
        user = self._profile(email)
        store.save({"email": email, "token": token, "user_id": self.user_id})
        print("Fuelin session saved in macOS Keychain. Password was not saved.")
        return user

    def _profile(self, email):
        user = self.request("GET", "/api/v1/users/" + self.user_id)
        if not isinstance(user, dict) or user.get("id") != self.user_id or str(user.get("email", "")).casefold() != email.casefold():
            raise SyncError("Profile did not match login identity")
        return user


def workout_instant(value, zone):
    """Fuelin's timezone-less timestamps are wall times in the account timezone."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=zone) if parsed.utcoffset() is None else parsed


def activity_family(kind):
    if kind in ("bike", "ride"):
        return "bike"
    if kind in ("gym", "strength", "weights"):
        return "gym"
    return kind


def same_workout(existing, body, zone=ZoneInfo("UTC")):
    try:
        return (activity_family(existing.get("type")) == activity_family(body["type"]) and existing.get("intensity") == body["intensity"]
                and all(workout_instant(existing[key], zone) == workout_instant(body[key], zone) for key in ("startsAt", "endsAt")))
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def conflicting_workout(existing, body, zone=ZoneInfo("UTC")):
    """Treat bike/ride as aliases; a possible manual match must stop creation."""
    if activity_family(existing.get("type")) != activity_family(body["type"]):
        return False
    try:
        def instant(value):
            return workout_instant(value, zone)
        return (instant(existing["startsAt"]) < instant(body["endsAt"])
                and instant(body["startsAt"]) < instant(existing["endsAt"]))
    except (KeyError, TypeError, ValueError, AttributeError):
        # Unknown timing on an activity of the same type cannot rule out a duplicate.
        return True


def time_field_details(existing):
    """Only show recognised temporal values from the documented time fields."""
    lines = []
    for key in ("startsAt", "endsAt", "effectiveStartsAt", "effectiveEndsAt",
                "plannedStartsAt", "plannedEndsAt", "userStartTime", "userEndTime",
                "userPlannedStartTime", "userPlannedEndTime",
                "completedStartsAt", "completedEndsAt", "userCompletedStartTime", "userCompletedEndTime"):
        value = existing.get(key)
        if value is None:
            continue
        label = "unrecognised format (value hidden)"
        if isinstance(value, str) and len(value) <= 40:
            try:
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?", value):
                    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                    suffix = "timezone provided" if parsed.utcoffset() is not None else "timezone unspecified"
                    label = f"{parsed.isoformat()} ({suffix})"
                elif re.fullmatch(r"\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?", value):
                    label = f"{time.fromisoformat(value).isoformat()} (time only; timezone unspecified)"
            except ValueError:
                pass
        lines.append(f"{key}: {label}")
    return lines or ["No populated time fields returned."]


def comparison_details(existing, body, zone):
    """Describe only workout fields needed to review a potential manual match."""
    details = []
    kind = existing.get("type")
    safe_types = {"bike", "ride", "gym", "strength", "weights", "run", "swim"}
    details.append("Activity type: " + (kind if isinstance(kind, str) and kind in safe_types else "other/unknown"))
    intensity = existing.get("intensity")
    details.append("Intensity: " + (str(intensity) if type(intensity) is int and 1 <= intensity <= 5 else "unknown"))
    unknown = False
    for key, label in (("startsAt", "Start"), ("endsAt", "End")):
        try:
            instant = workout_instant(existing[key], zone)
            expected = workout_instant(body[key], zone)
            suffix = "matches" if instant == expected else "differs"
            details.append(f"{label}: {instant.astimezone(zone).isoformat()} ({suffix})")
        except (KeyError, ValueError, TypeError, AttributeError):
            details.append(f"{label}: unknown")
            unknown = True
    if unknown:
        details.append("Returned time fields (no timezone assumptions applied):")
        details.extend(time_field_details(existing))
    return details


def reconcile_pending(client, entry, database, zone=ZoneInfo("UTC")):
    """Recover a pending create using GET only; never repeat a POST."""
    if not Path(database).exists():
        raise SyncError("No local state exists to reconcile")
    with closing(sqlite3.connect(database)) as db:
        row = db.execute("SELECT date,body,status FROM workouts WHERE user_id=? AND source_uid=?",
                         (client.user_id, entry["source_uid"])).fetchone()
        if not row or row[2] not in ("pending", "submitted"):
            raise SyncError("No pending creation exists for this source workout")
        day, raw_body, _ = row
        body = json.loads(raw_body)
        route = f"/api/v1/users/{client.user_id}/days/{quote(day, safe='')}/workouts"
        existing = client.request("GET", route)
        if not isinstance(existing, list):
            raise SyncError("Unexpected workout list; request state retained")
        matches = [workout for workout in existing if isinstance(workout, dict) and same_workout(workout, body, zone)]
        if len(matches) != 1:
            print(f"Pending request date: {day}; Fuelin activities returned: {len(existing)}.")
            for index, workout in enumerate(existing, 1):
                if isinstance(workout, dict):
                    print(f"Existing activity {index}:")
                    for line in comparison_details(workout, body, zone):
                        print("  " + line)
            raise SyncError(f"Found {len(matches)} exact matches for the pending request; state retained, no POST repeated")
        try:
            uid = str(UUID(matches[0]["id"]))
        except (KeyError, TypeError, ValueError, AttributeError):
            raise SyncError("Matching workout lacks a valid ID; pending state retained") from None
        other = db.execute("SELECT source_uid FROM workouts WHERE user_id=? AND fuelin_id=? AND source_uid<>?",
                           (client.user_id, uid, entry["source_uid"])).fetchone()
        if other:
            raise SyncError("Matching Fuelin workout is linked to another source; pending state retained")
        db.execute("UPDATE workouts SET status='created',fuelin_id=? WHERE user_id=? AND source_uid=? AND status IN ('pending','submitted')",
                   (uid, client.user_id, entry["source_uid"]))
        db.commit()
        return uid


def create_once(client, entry, database, zone=ZoneInfo("UTC"), existing=None):
    """Read once (or use weekly snapshot), reserve, POST once, record submission."""
    descriptor = os.open(database, os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(descriptor)
    with closing(sqlite3.connect(database)) as db:
        db.execute("CREATE TABLE IF NOT EXISTS workouts (user_id TEXT, source_uid TEXT, date TEXT, body TEXT, status TEXT, fuelin_id TEXT, PRIMARY KEY(user_id, source_uid))")
        row = db.execute("SELECT status FROM workouts WHERE user_id=? AND source_uid=?", (client.user_id, entry["source_uid"])).fetchone()
        if row:
            raise SyncError("This source workout is already recorded or pending; no POST attempted")
        route = f"/api/v1/users/{client.user_id}/days/{quote(entry['date'], safe='')}/workouts"
        if existing is None:
            existing = client.request("GET", route)
        if not isinstance(existing, list):
            raise SyncError("Unexpected workout list; no POST attempted")
        if any(same_workout(workout, entry["body"], zone) for workout in existing if isinstance(workout, dict)):
            raise SyncError("An equivalent workout already exists in Fuelin; no POST attempted")
        if any(conflicting_workout(workout, entry["body"], zone) for workout in existing if isinstance(workout, dict)):
            raise SyncError("A same-type activity overlaps this workout or has unknown timing; possible manual duplicate. No POST attempted")
        # Atomic unique reservation also stops concurrent attempts for the same source.
        db.execute("INSERT INTO workouts VALUES (?, ?, ?, ?, 'pending', NULL)",
                   (client.user_id, entry["source_uid"], entry["date"], json.dumps(entry["body"], sort_keys=True)))
        db.commit()
        result = client.request("POST", route, entry["body"])
        if isinstance(result, dict) and (result.get("success") is False or
                (type(result.get("status")) in (int, float) and result["status"] >= 400)):
            raise SyncError("Fuelin reported an error in its create response; request record retained")
        uid = None
        if isinstance(result, dict) and isinstance(result.get("id"), str):
            try:
                uid = str(UUID(result["id"]))
            except ValueError:
                pass
        db.execute("UPDATE workouts SET status='submitted',fuelin_id=? WHERE user_id=? AND source_uid=?",
                   (uid, client.user_id, entry["source_uid"]))
        db.commit()
        return uid


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--date", required=True, help="Local workout date YYYY-MM-DD")
    parser.add_argument("--title", required=True, help="Exact TrainerRoad workout title")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--check-existing", action="store_true", help="Log in and compare existing workouts without writing")
    mode.add_argument("--reconcile", action="store_true", help="Resolve a pending create using reads; update local state only")
    args = parser.parse_args()
    try:
        preview = json.loads(Path("preview.json").read_text())
        plan = build_plan(preview, load_config(args.config), json.loads(Path("fuelin-schema.json").read_text()))
        matches = [entry for entry in plan["entries"] if entry["date"] == args.date and entry["title"] == args.title]
        if len(matches) != 1 or matches[0]["status"] != "ready_for_review":
            raise SyncError("Select exactly one resolved workout from preview.json")
        entry = matches[0]
        zone = ZoneInfo(plan["timezone"])
        starts = workout_instant(entry["body"]["startsAt"], zone)
        if starts.astimezone(zone).date().isoformat() != entry["date"]:
            raise SyncError("Local date and start timestamp disagree")
        if args.apply and starts <= datetime.now(timezone.utc):
            raise SyncError("Only future workouts can be created")
        print(f"{entry['date']} {entry['title']}: {entry['body']['type']}, intensity {entry['body']['intensity']}")
        print(f"Start: {starts.astimezone(zone).isoformat()}; end: {workout_instant(entry['body']['endsAt'], zone).astimezone(zone).isoformat()}")
        if not args.apply and not args.check_existing and not args.reconcile:
            print("Dry run. Use --apply to create this one workout.")
            return 0
        client = FuelinClient()
        user = client.login()
        if user.get("timezone") and user["timezone"] != plan["timezone"]:
            raise SyncError("Fuelin timezone differs from preview; resolve before writing")
        if args.reconcile:
            reconcile_pending(client, entry, "sync-state.sqlite3", zone)
            print("Reconciled and verified the existing workout. Local record updated; no Fuelin activities were changed.")
            return 0
        if args.check_existing:
            route = f"/api/v1/users/{client.user_id}/days/{entry['date']}/workouts"
            existing = client.request("GET", route)
            if not isinstance(existing, list):
                raise SyncError("Unexpected workout list")
            exact = sum(same_workout(workout, entry["body"], zone) for workout in existing if isinstance(workout, dict))
            possible = sum(conflicting_workout(workout, entry["body"], zone) for workout in existing if isinstance(workout, dict))
            print(f"Existing activities on this date: {len(existing)}; exact matches: {exact}; overlapping or uncertain same-type matches: {possible}.")
            for index, workout in enumerate((workout for workout in existing
                                             if isinstance(workout, dict) and conflicting_workout(workout, entry["body"], zone)), 1):
                print(f"Possible match {index}:")
                for line in comparison_details(workout, entry["body"], zone):
                    print("  " + line)
            print("Read-only check complete. No activities or sync records were changed.")
            return 0
        create_once(client, entry, "sync-state.sqlite3", zone)
        print("Create request submitted. No post-create reads were made. Check the session in Fuelin or on the next weekly run.")
        return 0
    except Exception as error:
        # Network and response exceptions can contain secrets; never echo them.
        if isinstance(error, (SyncError, StorageError, ConfigError)):
            print(str(error))
        else:
            print("Request failed. No automatic retry was made.")
        print("If a create request was sent, its local reservation prevents another POST. Keep sync-state.sqlite3.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
