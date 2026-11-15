import unittest
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from create_workout import create_workout, SyncError, comparison_details, same_workout, time_field_details


class CreateTests(unittest.TestCase):
    def setUp(self):
        self.zone = ZoneInfo('Australia/Sydney')
        self.body = {'type': 'bike', 'intensity': 4, 'startsAt': '2026-11-17T06:00:00', 'endsAt': '2026-11-17T07:00:00'}
        self.entry = {'source_uid': 'tr1', 'date': '2026-11-17', 'body': self.body}
        self.client = MagicMock(user_id='own')

    def test_single_workout_reads_then_posts_without_verification(self):
        self.client.request.side_effect = [[], {'status': 200}]
        create_workout(self.client, self.entry, self.zone)
        self.assertEqual([call.args[0] for call in self.client.request.call_args_list], ['GET', 'POST'])

    def test_shared_snapshot_avoids_all_reads_during_write_phase(self):
        self.client.request.return_value = {}
        create_workout(self.client, self.entry, self.zone, existing=[])
        self.assertEqual([call.args[0] for call in self.client.request.call_args_list], ['POST'])

    def test_visible_match_or_overlap_stops_duplicate_create(self):
        for changes in [{}, {'intensity': 2}, {'type': 'ride'}]:
            self.client.reset_mock()
            with self.subTest(changes=changes), self.assertRaises(SyncError):
                create_workout(self.client, self.entry, self.zone, existing=[{**self.body, **changes}])
            self.client.request.assert_not_called()

    def test_timeout_is_not_retried(self):
        self.client.request.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            create_workout(self.client, self.entry, self.zone, existing=[])
        self.assertEqual(self.client.request.call_count, 1)

    def test_naive_and_aware_times_match_in_account_timezone(self):
        aware = {**self.body, 'startsAt': '2026-11-16T19:00:00Z', 'endsAt': '2026-11-16T20:00:00Z'}
        self.assertTrue(same_workout(aware, self.body, self.zone))
        self.assertIn('Start: 2026-11-17T06:00:00+11:00 (matches)', comparison_details(aware, self.body, self.zone))

    def test_unknown_temporal_values_are_not_printed(self):
        text = '\n'.join(time_field_details({'startsAt': 'private-token', 'userStartTime': '06:00'}))
        self.assertNotIn('private-token', text)
        self.assertIn('06:00:00', text)
