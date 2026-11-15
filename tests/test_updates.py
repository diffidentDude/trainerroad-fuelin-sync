import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from create_workout import SyncError
from sync_week import execute_week, plan_week


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.zone = ZoneInfo('Australia/Sydney')
        self.now = datetime(2026, 11, 15, 20, tzinfo=self.zone)
        self.uid = '12345678-1234-5678-1234-567812345678'
        self.client = MagicMock(user_id='own')
        self.body = {'type': 'bike', 'intensity': 4, 'startsAt': '2026-11-17T06:00:00', 'endsAt': '2026-11-17T07:00:00'}
        self.entry = {'source_uid': 'tr1', 'date': '2026-11-17', 'title': 'Ride', 'status': 'ready_for_review', 'body': self.body}

    def test_duration_intensity_start_and_type_changes_patch_existing_id(self):
        for changes in [{'endsAt': '2026-11-17T07:30:00'}, {'intensity': 3},
                        {'startsAt': '2026-11-17T09:00:00', 'endsAt': '2026-11-17T10:00:00'}, {'type': 'run'}]:
            with self.subTest(changes=changes):
                entry = {**self.entry, 'body': {**self.body, **changes}}
                self.client.reset_mock()
                self.client.request.side_effect = [[{'id': self.uid, **self.body}], {}]
                actions = plan_week(self.client, [entry], self.zone, self.now)
                self.assertEqual(actions[0]['action'], 'update')
                with patch('sync_week.datetime') as clock, redirect_stdout(io.StringIO()):
                    clock.now.return_value = self.now
                    self.assertEqual(execute_week(self.client, actions, self.zone)['updated'], 1)
                calls = self.client.request.call_args_list
                self.assertEqual([call.args[0] for call in calls], ['GET', 'PATCH'])
                self.assertEqual(calls[-1].args[2], {**entry['body'], 'workoutId': self.uid})

    def test_ambiguous_completed_or_recurring_matches_require_review(self):
        for values in [[{'id': self.uid, **self.body, 'intensity': 2}, {'id': 'other', **self.body, 'intensity': 3}],
                       [{'id': self.uid, **self.body, 'intensity': 2, 'isCompleted': True}],
                       [{'id': self.uid, **self.body, 'intensity': 2, 'recurringActivityId': 12}]]:
            self.client.request.return_value = values
            with self.subTest(values=values):
                self.assertEqual(plan_week(self.client, [self.entry], self.zone, self.now)[0]['action'], 'review')

    def test_visible_update_is_skipped_on_next_run_without_local_history(self):
        self.client.request.return_value = [{'id': self.uid, **self.body}]
        actions = plan_week(self.client, [self.entry], self.zone, self.now)
        self.assertEqual(actions[0]['action'], 'matched')
        self.assertEqual(execute_week(self.client, actions, self.zone)['matched'], 1)
        self.assertEqual([call.args[0] for call in self.client.request.call_args_list], ['GET'])

    def test_patch_contract_matches_published_schema(self):
        schema = json.loads(Path('fuelin-schema.json').read_text())['components']['schemas']['UpdateWorkout']
        body = {**self.body, 'workoutId': self.uid}
        self.assertTrue(set(schema['required']) <= set(body) <= set(schema['properties']))
