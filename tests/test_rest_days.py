import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from unittest.mock import MagicMock, patch
from pathlib import Path
from zoneinfo import ZoneInfo

from create_workout import FuelinClient, SyncError
from sync_week import execute_week, main, plan_week


class RestDayTests(unittest.TestCase):
    def setUp(self):
        self.zone = ZoneInfo('UTC')
        self.now = datetime(2026, 11, 20, tzinfo=self.zone)
        self.day = '2026-11-22'
        self.uid = '12345678-1234-5678-1234-567812345678'
        self.activity = {'id': self.uid, 'type': 'bike', 'intensity': 4,
                         'startsAt': self.day + 'T07:00:00', 'endsAt': self.day + 'T08:00:00',
                         'isCompleted': False}
        self.client = MagicMock(user_id='own')

    def test_rest_day_without_workouts_still_reads_fuelin(self):
        self.client.request.return_value = []
        self.assertEqual(plan_week(self.client, [], self.zone, self.now, {self.day}), [])
        self.assertIn('/days/' + self.day + '/workouts', self.client.request.call_args.args[1])

    def test_default_policy_requires_review_before_any_write(self):
        self.client.request.return_value = [self.activity]
        actions = plan_week(self.client, [], self.zone, self.now, {self.day})
        self.assertEqual(actions[0]['action'], 'review')
        with self.assertRaises(SyncError):
            execute_week(self.client, actions, self.zone)
        self.assertEqual(self.client.request.call_count, 1)

    def test_clear_planned_day_keeps_completed_activities(self):
        gym = {**self.activity, 'id': '23456789-1234-5678-1234-567812345678', 'type': 'strength'}
        completed = {**self.activity, 'id': 'completed', 'isCompleted': True}
        imported = {**self.activity, 'id': 'imported', 'completedSource': 'garmin'}
        self.client.request.side_effect = [[self.activity, gym, completed, imported], None, None]
        actions = plan_week(self.client, [], self.zone, self.now, {self.day}, 'delete_all_planned')
        self.assertEqual([action['action'] for action in actions], ['delete', 'delete'])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(execute_week(self.client, actions, self.zone)['deleted'], 2)
        self.assertEqual([call.args[0] for call in self.client.request.call_args_list], ['GET', 'DELETE', 'DELETE'])

    def test_cycling_only_policy_keeps_gym(self):
        self.client.request.return_value = [self.activity, {**self.activity, 'type': 'strength', 'id': 'gym'}]
        actions = plan_week(self.client, [], self.zone, self.now, {self.day}, 'delete_cycling')
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]['fuelin_id'], self.uid)

    def test_conflicting_source_workout_and_rest_marker_need_review(self):
        self.client.request.return_value = [self.activity]
        entry = {'date': self.day, 'source_uid': 'tr1', 'title': 'Ride', 'status': 'ready_for_review',
                 'body': {key: self.activity[key] for key in ('type', 'intensity', 'startsAt', 'endsAt')}}
        actions = plan_week(self.client, [entry], self.zone, self.now, {self.day}, 'delete_all_planned')
        self.assertEqual([item['action'] for item in actions], ['review'])

    def test_past_rest_days_never_delete_activities(self):
        self.client.request.return_value = [self.activity]
        now = datetime(2026, 11, 23, tzinfo=self.zone)
        self.assertEqual(plan_week(self.client, [], self.zone, now, {self.day}, 'delete_all_planned'), [])

    def test_rest_only_feed_clears_planned_workout_without_creates_or_verification(self):
        self.client.login.return_value = {'timezone': 'UTC'}
        self.client.request.side_effect = [[{'date': self.day}], [], [self.activity], None]
        with tempfile.TemporaryDirectory() as directory:
            feed = Path(directory) / 'calendar.ics'
            feed.write_text('BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:rest\nDTSTART;VALUE=DATE:20261122\nSUMMARY: Rest Day \nEND:VEVENT\nEND:VCALENDAR\n')
            config = Path(directory) / 'config.json'
            config.write_text(json.dumps({'timezone': 'UTC', 'rest_day_action': 'delete_all_planned'}))
            with patch('sys.argv', ['sync.py', '--file', str(feed), '--config', str(config), '--apply']), patch('sync_week.datetime') as clock, patch('sync_week.FuelinClient', return_value=self.client), redirect_stdout(io.StringIO()) as output:
                clock.now.return_value = self.now
                self.assertEqual(main(), 0, output.getvalue())
            self.assertIn('1 rest-day deletions submitted', output.getvalue())
        self.assertEqual([call.args[0] for call in self.client.request.call_args_list], ['GET', 'GET', 'GET', 'DELETE'])

    def test_empty_delete_response_is_accepted_and_marks_write_attempted(self):
        client = FuelinClient()
        client.opener = MagicMock()
        client.opener.open.return_value.__enter__.return_value.read.return_value = b''
        self.assertIsNone(client.request('DELETE', '/api/v1/users/own/days/2026-11-22/workouts/' + self.uid))
        self.assertTrue(client.workout_write_attempted)
