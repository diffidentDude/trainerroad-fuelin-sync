import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from zoneinfo import ZoneInfo

from create_workout import SyncError, create_once
from sync_week import execute_week, load_records, plan_week, week_start, main, available_dates


class WeekTests(unittest.TestCase):
    def test_day_discovery_paginates_and_preserves_gaps(self):
        self.client.request.side_effect = [[{"date": "2026-11-17T00:00:00"}], [{"date": "2026-11-19"}], []]
        self.assertEqual(available_dates(self.client, "2026-11-16", "2026-11-22"), {"2026-11-17", "2026-11-19"})
        self.assertIn("offset=1", self.client.request.call_args_list[1].args[1])
        self.assertIn("offset=2", self.client.request.call_args_list[2].args[1])

    def test_repeated_or_malformed_day_pages_abort_without_writes(self):
        for pages in [[[{"date": "2026-11-17"}], [{"date": "2026-11-17"}]], [[{"date": "not-a-date"}]], [{"days": []}]]:
            self.client.reset_mock()
            self.client.request.side_effect = pages
            with self.subTest(pages=pages), self.assertRaises(SyncError):
                available_dates(self.client, "2026-11-16", "2026-11-22")
            self.assertTrue(all(call.args[0] == "GET" for call in self.client.request.call_args_list))
    def test_404_is_not_silently_treated_as_empty_workout_list(self):
        self.client.request.side_effect = [HTTPError("https://prod.fuelin.io", 404, "missing", {}, io.BytesIO()), []]
        with self.assertRaisesRegex(SyncError, "no day record for 2026-11-17"):
            plan_week(self.client, [self.entry], {}, self.zone, self.now)
        self.assertEqual([call.args[0] for call in self.client.request.call_args_list], ["GET", "GET"])
        self.assertIn("from=2026-11-17", self.client.request.call_args_list[1].args[1])

    def test_404_for_existing_day_requires_investigation(self):
        self.client.request.side_effect = [HTTPError("https://prod.fuelin.io", 404, "missing", {}, io.BytesIO()), [{"date": "2026-11-17"}]]
        with self.assertRaisesRegex(SyncError, "day lookup was not empty"):
            plan_week(self.client, [self.entry], {}, self.zone, self.now)
        self.assertTrue(all(call.args[0] == "GET" for call in self.client.request.call_args_list))
    def test_default_scans_all_future_workouts_but_only_sends_open_dates(self):
        sample = Path("examples/sample.txt").read_text().replace("over-under intervals.", "over-under cycling intervals at FTP.")
        distant = sample.replace("20261117", "20270302").replace("example-vennacher", "far-future")
        self.client.login.return_value = {"timezone": "Australia/Sydney"}
        # Far-future plans become eligible when Fuelin actually exposes those days.
        for open_days in [[], ["2026-11-17"], ["2026-11-17", "2027-03-02"]]:
            with self.subTest(open_days=open_days), tempfile.TemporaryDirectory() as directory:
                self.client.reset_mock()
                pages = [[{"date": day} for day in open_days], []] if open_days else [[]]
                self.client.request.side_effect = pages + [[] for _ in open_days] + [{"status": 200} for _ in open_days]
                path = Path(directory) / "feed.ics"
                far_event = "BEGIN:VEVENT" + distant.split("BEGIN:VEVENT", 1)[1]
                path.write_text(sample.replace("END:VCALENDAR", far_event))
                state = str(Path(directory) / "state")
                with patch("sys.argv", ["sync.py", "--file", str(path), "--apply"]), patch("sync_week.datetime") as clock, patch("sync_week.FuelinClient", return_value=self.client), patch("sync_week.load_records", return_value={}), patch("sync_week.execute_week", side_effect=lambda client, actions, database, zone: execute_week(client, actions, state, zone)), redirect_stdout(io.StringIO()) as output:
                    clock.now.return_value = self.now
                    result = main()
                    self.assertEqual(result, 0, output.getvalue())
                posts = [call for call in self.client.request.call_args_list if call.args[0] == "POST"]
                self.assertEqual(len(posts), len(open_days))
                for call, day in zip(posts, open_days):
                    self.assertIn(f"/days/{day}/workouts", call.args[1])
                methods = [call.args[0] for call in self.client.request.call_args_list]
                self.assertEqual(methods, ["GET"] * (len(pages) + len(open_days)) + ["POST"] * len(open_days))

    def setUp(self):
        self.zone = ZoneInfo("Australia/Sydney")
        self.now = datetime(2026, 11, 15, 20, tzinfo=self.zone)
        self.client = MagicMock()
        self.client.user_id = "own"
        self.entry = {"source_uid": "tr1", "date": "2026-11-17", "title": "Galena -2", "status": "ready_for_review",
                      "body": {"type": "bike", "intensity": 4, "startsAt": "2026-11-17T06:00:00", "endsAt": "2026-11-17T07:00:00"}}

    def test_next_week_on_sunday_and_explicit_monday(self):
        self.assertEqual(week_start("next", date(2026, 11, 15)), date(2026, 11, 16))
        self.assertEqual(week_start("current", date(2026, 11, 15)), date(2026, 11, 9))
        self.assertEqual(week_start("2026-11-16", date(2026, 11, 15)), date(2026, 11, 16))
        with self.assertRaises(SyncError):
            week_start("2026-11-17", date(2026, 11, 15))

    def test_mix_of_manual_tracked_and_missing_workouts(self):
        gym = {**self.entry, "source_uid": "gym", "title": "Gym", "body": {**self.entry["body"], "type": "gym", "intensity": 1, "startsAt": "2026-11-17T12:00:00", "endsAt": "2026-11-17T12:30:00"}}
        missing = {**self.entry, "source_uid": "tr2", "date": "2026-11-19", "body": {**self.entry["body"], "startsAt": "2026-11-19T06:00:00", "endsAt": "2026-11-19T07:00:00"}}
        self.client.request.side_effect = [[{"id": "saved", **self.entry["body"]}, {"id": "manual", **gym["body"]}], []]
        records = {"tr1": {"status": "created", "date": "2026-11-17", "fuelin_id": "saved"}}
        actions = plan_week(self.client, [self.entry, gym, missing], records, self.zone, self.now)
        self.assertEqual([item["action"] for item in actions], ["tracked", "manual", "create"])
        self.assertTrue(all(call.args[0] == "GET" for call in self.client.request.call_args_list))

    def test_conflicting_manual_intensity_plans_update(self):
        self.client.request.return_value = [{"id": "manual", **self.entry["body"], "intensity": 2}]
        actions = plan_week(self.client, [self.entry], {}, self.zone, self.now)
        self.assertEqual(actions[0]["action"], "update")
        self.assertIn("Intensity: 2", "\n".join(actions[0]["details"]))

    def test_strength_alias_matches_gym_and_duration_difference_updates(self):
        entry = {**self.entry, "body": {"type": "gym", "intensity": 1, "startsAt": "2026-11-17T12:00:00", "endsAt": "2026-11-17T12:30:00"}}
        existing = {"id": "manual-gym", **entry["body"], "type": "strength"}
        self.client.request.return_value = [existing]
        self.assertEqual(plan_week(self.client, [entry], {}, self.zone, self.now)[0]["action"], "manual")
        self.client.request.return_value = [{**existing, "endsAt": "2026-11-17T12:35:00"}]
        self.assertEqual(plan_week(self.client, [entry], {}, self.zone, self.now)[0]["action"], "update")
        preserved = plan_week(self.client, [entry], {}, self.zone, self.now, keep_manual_gym=True)[0]
        self.assertEqual(preserved["action"], "update")
        self.assertTrue(all(call.args[0] == "GET" for call in self.client.request.call_args_list))

    def test_keep_gym_option_does_not_hide_other_differences(self):
        entry = {**self.entry, "body": {"type": "gym", "intensity": 1, "startsAt": "2026-11-17T12:00:00", "endsAt": "2026-11-17T12:30:00"}}
        for changes in [{"intensity": 2}, {"startsAt": "2026-11-17T12:05:00"}, {"endsAt": None}]:
            self.client.request.return_value = [{"id": "manual", **entry["body"], **changes}]
            with self.subTest(changes=changes):
                self.assertEqual(plan_week(self.client, [entry], {}, self.zone, self.now, keep_manual_gym=True)[0]["action"], "update")
        self.client.request.return_value = [{"id": "one", **entry["body"], "endsAt": "2026-11-17T12:35:00"},
                                            {"id": "two", **entry["body"], "endsAt": "2026-11-17T12:40:00"}]
        self.assertEqual(plan_week(self.client, [entry], {}, self.zone, self.now, keep_manual_gym=True)[0]["action"], "review")

    def test_pending_record_is_planned_for_read_only_recovery(self):
        self.client.request.return_value = []
        actions = plan_week(self.client, [self.entry], {"tr1": {"status": "pending", "date": self.entry["date"], "body": json.dumps(self.entry["body"])}}, self.zone, self.now)
        self.assertEqual(actions[0]["action"], "uncertain")

    def test_initial_reads_then_posts_without_verification(self):
        later = {**self.entry, "source_uid": "later", "date": "2026-11-19",
                 "body": {**self.entry["body"], "startsAt": "2026-11-19T06:00:00", "endsAt": "2026-11-19T07:00:00"}}
        self.client.request.side_effect = [[], [], {"status": 200}, {"status": 200}]
        with tempfile.TemporaryDirectory() as directory, patch("sync_week.datetime") as clock, redirect_stdout(io.StringIO()):
            clock.now.return_value = self.now
            actions = plan_week(self.client, [self.entry, later], {}, self.zone, self.now)
            counts = execute_week(self.client, actions, str(Path(directory) / "state"), self.zone)
        self.assertEqual(counts["submitted"], 2)
        self.assertEqual([call.args[0] for call in self.client.request.call_args_list], ["GET", "GET", "POST", "POST"])

    def test_already_sent_but_not_visible_does_not_block_or_repeat_post(self):
        record = {"status": "submitted", "date": self.entry["date"], "body": json.dumps(self.entry["body"])}
        self.client.request.return_value = []
        actions = plan_week(self.client, [self.entry], {"tr1": record}, self.zone, self.now)
        self.assertEqual(actions[0]["action"], "submitted")
        with redirect_stdout(io.StringIO()):
            counts = execute_week(self.client, actions, "unused", self.zone)
        self.assertEqual(counts["previously_sent"], 1)
        self.assertEqual([call.args[0] for call in self.client.request.call_args_list], ["GET"])

    def test_uncertain_old_request_does_not_block_sunday_create(self):
        record = {"status": "pending", "date": self.entry["date"], "body": json.dumps(self.entry["body"])}
        sunday = {**self.entry, "source_uid": "sunday", "date": "2026-11-22",
                  "body": {**self.entry["body"], "startsAt": "2026-11-22T07:00:00", "endsAt": "2026-11-22T07:30:00"}}
        self.client.request.side_effect = [[], [], {"status": 200}]
        with tempfile.TemporaryDirectory() as directory, patch("sync_week.datetime") as clock, redirect_stdout(io.StringIO()):
            clock.now.return_value = self.now
            actions = plan_week(self.client, [self.entry, sunday], {"tr1": record}, self.zone, self.now)
            counts = execute_week(self.client, actions, str(Path(directory) / "state"), self.zone)
        self.assertEqual(counts["submitted"], 1)
        self.assertEqual(counts["uncertain"], 1)
        self.assertEqual([call.args[0] for call in self.client.request.call_args_list], ["GET", "GET", "POST"])

    def test_prior_pending_is_linked_from_initial_snapshot_without_further_reads(self):
        self.client.request.side_effect = [[], TimeoutError()]
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "state")
            with self.assertRaises(TimeoutError):
                create_once(self.client, self.entry, database, self.zone)
            self.client.reset_mock()
            uid = "12345678-1234-5678-1234-567812345678"
            self.client.request.side_effect = [[{"id": uid, **self.entry["body"]}]]
            actions = plan_week(self.client, [self.entry], load_records(database, "own"), self.zone, self.now)
            self.assertEqual(actions[0]["action"], "confirm")
            with redirect_stdout(io.StringIO()):
                counts = execute_week(self.client, actions, database, self.zone)
            self.assertEqual(counts["confirmed"], 1)
            self.assertEqual(load_records(database, "own")["tr1"]["status"], "created")
        self.assertEqual([call.args[0] for call in self.client.request.call_args_list], ["GET"])

    def test_missing_tracked_activity_requires_review(self):
        self.client.request.return_value = []
        records = {"tr1": {"status": "created", "date": "2026-11-17", "fuelin_id": "saved"}}
        self.assertEqual(plan_week(self.client, [self.entry], records, self.zone, self.now)[0]["action"], "review")

    def test_two_source_sessions_cannot_consume_one_manual_entry(self):
        self.client.request.return_value = [{"id": "manual", **self.entry["body"]}]
        actions = plan_week(self.client, [self.entry, {**self.entry, "source_uid": "other"}], {}, self.zone, self.now)
        self.assertEqual([item["action"] for item in actions], ["manual", "review"])

    def test_overlapping_source_sessions_require_start_time_review(self):
        self.client.request.return_value = []
        actions = plan_week(self.client, [self.entry, {**self.entry, "source_uid": "other"}], {}, self.zone, self.now)
        self.assertEqual([item["action"] for item in actions], ["review", "review"])

    def test_invalid_remote_list_aborts_plan(self):
        self.client.request.return_value = {"unexpected": []}
        with self.assertRaises(SyncError):
            plan_week(self.client, [self.entry], {}, self.zone, self.now)

    def test_execute_skips_existing_and_stops_on_uncertain_create(self):
        with patch("sync_week.create_once", side_effect=TimeoutError()) as create, patch("sync_week.datetime") as clock:
            clock.now.return_value = self.now
            with self.assertRaises(TimeoutError), redirect_stdout(io.StringIO()):
                execute_week(self.client, [{"action": "tracked", "entry": self.entry},
                                          {"action": "create", "entry": self.entry, "snapshot": []},
                                          {"action": "create", "entry": self.entry}], "unused", self.zone)
            self.assertEqual(create.call_count, 1)

    def test_missing_state_read_does_not_create_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.sqlite3"
            self.assertEqual(load_records(path, "own"), {})
            self.assertFalse(path.exists())
