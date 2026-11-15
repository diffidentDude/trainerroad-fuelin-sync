import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from create_workout import SyncError
from sync_week import execute_week, load_records, plan_week


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.zone = ZoneInfo("Australia/Sydney")
        self.now = datetime(2026, 11, 15, 20, tzinfo=self.zone)
        self.uid = "12345678-1234-5678-1234-567812345678"
        self.client = MagicMock()
        self.client.user_id = "own"
        self.body = {"type": "bike", "intensity": 4, "startsAt": "2026-11-17T06:00:00", "endsAt": "2026-11-17T07:00:00"}
        self.entry = {"source_uid": "tr1", "date": "2026-11-17", "title": "Ride", "status": "ready_for_review", "body": self.body}

    def test_tracked_duration_intensity_start_and_type_changes_patch_same_id(self):
        for changes in [{"endsAt": "2026-11-17T07:30:00"}, {"intensity": 3},
                        {"startsAt": "2026-11-17T09:00:00", "endsAt": "2026-11-17T10:00:00"}, {"type": "run"}]:
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as directory:
                entry = {**self.entry, "body": {**self.body, **changes}}
                old = {"id": self.uid, **self.body}
                self.client.reset_mock()
                self.client.request.side_effect = [[old], {"status": 200}]
                records = {"tr1": {"status": "created", "date": self.entry["date"], "fuelin_id": self.uid}}
                actions = plan_week(self.client, [entry], records, self.zone, self.now)
                self.assertEqual(actions[0]["action"], "update")
                with patch("sync_week.datetime") as clock, redirect_stdout(io.StringIO()):
                    clock.now.return_value = self.now
                    counts = execute_week(self.client, actions, str(Path(directory) / "state"), self.zone)
                self.assertEqual(counts["updated"], 1)
                calls = self.client.request.call_args_list
                self.assertEqual([call.args[0] for call in calls], ["GET", "PATCH"])
                self.assertTrue(calls[-1].args[1].endswith("/" + self.uid))
                self.assertEqual(calls[-1].args[2], {**entry["body"], "workoutId": self.uid})

    def test_manual_duration_change_is_updated_and_linked(self):
        self.client.request.side_effect = [[{"id": self.uid, **self.body, "endsAt": "2026-11-17T07:35:00"}], {}]
        actions = plan_week(self.client, [self.entry], {}, self.zone, self.now)
        self.assertEqual(actions[0]["action"], "update")
        with tempfile.TemporaryDirectory() as directory, patch("sync_week.datetime") as clock, redirect_stdout(io.StringIO()):
            clock.now.return_value = self.now
            path = str(Path(directory) / "state")
            execute_week(self.client, actions, path, self.zone)
            record = load_records(path, "own")["tr1"]
            self.assertEqual(record["fuelin_id"], self.uid)
            self.assertEqual(record["status"], "submitted")
            self.assertEqual(json.loads(record["body"]), self.body)

    def test_next_run_confirms_update_without_more_patch_requests(self):
        self.client.request.side_effect = [[{"id": self.uid, **self.body, "intensity": 2}], {}]
        actions = plan_week(self.client, [self.entry], {}, self.zone, self.now)
        with tempfile.TemporaryDirectory() as directory, patch("sync_week.datetime") as clock, redirect_stdout(io.StringIO()):
            clock.now.return_value = self.now
            path = str(Path(directory) / "state")
            execute_week(self.client, actions, path, self.zone)
            self.client.reset_mock()
            self.client.request.side_effect = [[{"id": self.uid, **self.body}]]
            actions = plan_week(self.client, [self.entry], load_records(path, "own"), self.zone, self.now)
            self.assertEqual(actions[0]["action"], "confirm")
            execute_week(self.client, actions, path, self.zone)
            self.assertEqual([call.args[0] for call in self.client.request.call_args_list], ["GET"])
            self.assertEqual(load_records(path, "own")["tr1"]["status"], "created")

    def test_pending_create_can_be_found_by_previous_body_then_updated(self):
        old = {**self.body, "endsAt": "2026-11-17T06:30:00"}
        records = {"tr1": {"status": "submitted", "date": self.entry["date"], "body": json.dumps(old), "fuelin_id": None}}
        self.client.request.return_value = [{"id": self.uid, **old}]
        self.assertEqual(plan_week(self.client, [self.entry], records, self.zone, self.now)[0]["action"], "update")

    def test_update_timeout_is_recorded_and_never_becomes_duplicate_create(self):
        old = {"id": self.uid, **self.body, "intensity": 2}
        self.client.request.side_effect = [[old], TimeoutError()]
        actions = plan_week(self.client, [self.entry], {}, self.zone, self.now)
        with tempfile.TemporaryDirectory() as directory, patch("sync_week.datetime") as clock, redirect_stdout(io.StringIO()):
            clock.now.return_value = self.now
            path = str(Path(directory) / "state")
            with self.assertRaises(TimeoutError):
                execute_week(self.client, actions, path, self.zone)
            records = load_records(path, "own")
            self.assertEqual(records["tr1"]["status"], "update_pending")
            self.client.request.side_effect = None
            self.client.request.return_value = [old]
            self.assertEqual(plan_week(self.client, [self.entry], records, self.zone, self.now)[0]["action"], "uncertain")
            self.client.request.return_value = [{"id": self.uid, **self.body}]
            self.assertEqual(plan_week(self.client, [self.entry], records, self.zone, self.now)[0]["action"], "confirm")

    def test_ambiguous_existing_sessions_and_completed_workouts_are_not_updated(self):
        for values in [[{"id": self.uid, **self.body, "intensity": 2}, {"id": "other", **self.body, "intensity": 3}],
                       [{"id": self.uid, **self.body, "intensity": 2, "isCompleted": True}],
                       [{"id": self.uid, **self.body, "intensity": 2, "recurringActivityId": 12}]]:
            self.client.request.return_value = values
            with self.subTest(values=values):
                self.assertEqual(plan_week(self.client, [self.entry], {}, self.zone, self.now)[0]["action"], "review")

    def test_patch_body_obeys_supplied_schema(self):
        schema = json.loads(Path("fuelin-schema.json").read_text())["components"]["schemas"]["UpdateWorkout"]
        body = {**self.body, "workoutId": self.uid}
        self.assertTrue(set(schema["required"]) <= set(body))
        self.assertTrue(set(body) <= set(schema["properties"]))
