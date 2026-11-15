import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from create_workout import create_once, SyncError, comparison_details, time_field_details, same_workout, reconcile_pending
from zoneinfo import ZoneInfo


class CreateTests(unittest.TestCase):
    def test_response_without_id_is_submitted_without_any_verification_reads(self):
        self.client.request.side_effect = [[], {"status": 200}]
        self.assertIsNone(create_once(self.client, self.entry, self.database, self.zone))
        self.assertEqual([call.args[0] for call in self.client.request.call_args_list], ["GET", "POST"])
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT status,fuelin_id FROM workouts").fetchone(), ("submitted", None))

    def test_weekly_snapshot_avoids_all_reads_during_create(self):
        self.client.request.return_value = {"status": 200}
        create_once(self.client, self.entry, self.database, self.zone, existing=[])
        self.assertEqual([call.args[0] for call in self.client.request.call_args_list], ["POST"])

    def test_pending_request_reconciles_against_saved_body(self):
        self.client.request.side_effect = [[], TimeoutError()]
        with self.assertRaises(TimeoutError):
            create_once(self.client, self.entry, self.database, self.zone)
        self.client.reset_mock()
        result = {"id": "23456789-1234-5678-1234-567812345678", **self.entry["body"]}
        self.client.request.side_effect = [[result]]
        changed_entry = {**self.entry, "body": {**self.entry["body"], "intensity": 2}}
        self.assertEqual(reconcile_pending(self.client, changed_entry, self.database, self.zone), result["id"])
        self.assertEqual([call.args[0] for call in self.client.request.call_args_list], ["GET"])

    def test_ambiguous_reconciliation_keeps_pending_state(self):
        self.client.request.side_effect = [[], TimeoutError()]
        with self.assertRaises(TimeoutError):
            create_once(self.client, self.entry, self.database, self.zone)
        result = {"id": "23456789-1234-5678-1234-567812345678", **self.entry["body"]}
        self.client.request.side_effect = [[result, result]]
        with self.assertRaisesRegex(SyncError, "2 exact matches"):
            reconcile_pending(self.client, self.entry, self.database, self.zone)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT status FROM workouts").fetchone()[0], "pending")

    def test_missing_or_invalid_match_retains_pending_and_never_posts(self):
        self.client.request.side_effect = [[], TimeoutError()]
        with self.assertRaises(TimeoutError):
            create_once(self.client, self.entry, self.database, self.zone)
        for matches in [[], [{"id": "not-a-uuid", **self.entry["body"]}]]:
            self.client.reset_mock()
            self.client.request.side_effect = [matches]
            with self.subTest(matches=matches), self.assertRaises(SyncError):
                reconcile_pending(self.client, self.entry, self.database, self.zone)
            self.assertEqual([call.args[0] for call in self.client.request.call_args_list], ["GET"])
            with closing(sqlite3.connect(self.database)) as db:
                self.assertEqual(db.execute("SELECT status FROM workouts").fetchone()[0], "pending")

    def test_naive_manual_entry_matches_sydney_time_in_both_seasons(self):
        for month, utc_hour in [(11, 19), (7, 20)]:
            body = {"type": "bike", "intensity": 4,
                    "startsAt": f"2026-{month:02d}-16T{utc_hour}:00:00Z",
                    "endsAt": f"2026-{month:02d}-16T{utc_hour + 1}:00:00Z"}
            manual = {**body, "startsAt": f"2026-{month:02d}-17T06:00:00",
                      "endsAt": f"2026-{month:02d}-17T07:00:00"}
            with self.subTest(month=month):
                self.assertTrue(same_workout(manual, body, ZoneInfo("Australia/Sydney")))

    def test_manual_naive_match_blocks_creation(self):
        self.client.request.return_value = [{"type": "bike", "intensity": 4,
            "startsAt": "2026-11-17T06:00:00", "endsAt": "2026-11-17T07:00:00"}]
        with self.assertRaisesRegex(SyncError, "equivalent workout"):
            create_once(self.client, self.entry, self.database, self.zone)
        self.assertEqual(self.client.request.call_count, 1)

    def test_time_diagnostics_expose_naive_and_clock_formats_without_guessing(self):
        lines = time_field_details({"startsAt": "2026-11-17 06:00:00", "userStartTime": "06:00",
                                   "plannedStartsAt": "2026-11-16T19:00:00Z", "token": "secret"})
        self.assertIn("startsAt: 2026-11-17T06:00:00 (timezone unspecified)", lines)
        self.assertIn("userStartTime: 06:00:00 (time only; timezone unspecified)", lines)
        self.assertNotIn("secret", "\n".join(lines))

    def test_time_diagnostics_hide_unexpected_values(self):
        text = "\n".join(time_field_details({"startsAt": "private-token", "endsAt": None}))
        self.assertNotIn("private-token", text)
        self.assertIn("unrecognised format", text)

    def test_comparison_shows_local_time_and_intensity_difference(self):
        body = {"type": "bike", "intensity": 4, "startsAt": "2026-11-16T19:00:00Z", "endsAt": "2026-11-16T20:00:00Z"}
        result = comparison_details({**body, "intensity": 3}, body, ZoneInfo("Australia/Sydney"))
        self.assertIn("Intensity: 3", result)
        self.assertIn("Start: 2026-11-17T06:00:00+11:00 (matches)", result)

    def setUp(self):
        self.zone = ZoneInfo("Australia/Sydney")
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.database = str(Path(self.directory.name) / "state.sqlite3")
        self.entry = {"source_uid": "tr-uid", "date": "2026-11-17", "body": {
            "type": "bike", "intensity": 4, "startsAt": "2026-11-16T19:00:00Z", "endsAt": "2026-11-16T20:00:00Z"}}
        self.client = MagicMock()
        self.client.user_id = "12345678-1234-5678-1234-567812345678"

    def test_success_and_second_attempt_does_not_post(self):
        result = {"id": "23456789-1234-5678-1234-567812345678", **self.entry["body"]}
        self.client.request.side_effect = [[], result]
        self.assertEqual(create_once(self.client, self.entry, self.database, self.zone), result["id"])
        with self.assertRaises(SyncError):
            create_once(self.client, self.entry, self.database, self.zone)
        self.assertEqual([call.args[0] for call in self.client.request.call_args_list], ["GET", "POST"])

    def test_timeout_reserves_source_and_prevents_retry(self):
        self.client.request.side_effect = [[], TimeoutError()]
        with self.assertRaises(TimeoutError):
            create_once(self.client, self.entry, self.database, self.zone)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT status FROM workouts").fetchone()[0], "pending")
        with self.assertRaises(SyncError):
            create_once(self.client, self.entry, self.database, self.zone)
        self.assertEqual(self.client.request.call_count, 2)

    def test_existing_equivalent_is_not_duplicated(self):
        self.client.request.return_value = [{**self.entry["body"], "startsAt": "2026-11-17T06:00:00+11:00"}]
        with self.assertRaises(SyncError):
            create_once(self.client, self.entry, self.database, self.zone)
        self.assertEqual(self.client.request.call_count, 1)

    def test_invalid_list_stops_before_write(self):
        self.client.request.return_value = {"error": "unexpected"}
        with self.assertRaises(SyncError):
            create_once(self.client, self.entry, self.database, self.zone)
        self.assertEqual(self.client.request.call_count, 1)

    def test_manual_ride_with_different_intensity_is_not_duplicated(self):
        self.client.request.return_value = [{**self.entry["body"], "type": "ride", "intensity": 3}]
        with self.assertRaises(SyncError):
            create_once(self.client, self.entry, self.database, self.zone)
        self.assertEqual(self.client.request.call_count, 1)

    def test_same_type_unknown_time_blocks_creation(self):
        self.client.request.return_value = [{"type": "bike"}]
        with self.assertRaises(SyncError):
            create_once(self.client, self.entry, self.database, self.zone)
        self.assertEqual(self.client.request.call_count, 1)

    def test_nonoverlapping_activity_allows_separate_session(self):
        existing = {**self.entry["body"], "startsAt": "2026-11-17T01:00:00Z", "endsAt": "2026-11-17T02:00:00Z"}
        result = {"id": "23456789-1234-5678-1234-567812345678", **self.entry["body"]}
        self.client.request.side_effect = [[existing], result]
        self.assertEqual(create_once(self.client, self.entry, self.database, self.zone), result["id"])
