import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from zoneinfo import ZoneInfo

from create_workout import SyncError
from sync_week import available_dates, execute_week, main, plan_week, week_start


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.zone = ZoneInfo('Australia/Sydney')
        self.now = datetime(2026, 11, 15, 20, tzinfo=self.zone)
        self.client = MagicMock(user_id='own')
        self.body = {'type': 'bike', 'intensity': 4, 'startsAt': '2026-11-17T06:00:00', 'endsAt': '2026-11-17T07:00:00'}
        self.entry = {'source_uid': 'tr1', 'date': '2026-11-17', 'title': 'Ride', 'status': 'ready_for_review', 'body': self.body}

    def test_day_discovery_paginates_without_filling_gaps(self):
        self.client.request.side_effect = [[{'date': '2026-11-17T00:00:00'}], [{'date': '2026-11-19'}], []]
        self.assertEqual(available_dates(self.client, '2026-11-16', '2026-11-22'), {'2026-11-17', '2026-11-19'})
        self.assertIn('offset=1', self.client.request.call_args_list[1].args[1])

    def test_repeated_or_malformed_day_pages_fail_before_writes(self):
        for pages in [[[{'date': '2026-11-17'}], [{'date': '2026-11-17'}]], [[{'date': 'bad'}]], [{'days': []}]]:
            self.client.request.side_effect = pages
            with self.subTest(pages=pages), self.assertRaises(SyncError):
                available_dates(self.client, '2026-11-16', '2026-11-22')

    def test_404_is_not_treated_as_empty_workout_list(self):
        self.client.request.side_effect = [HTTPError('https://prod.fuelin.io', 404, 'missing', {}, io.BytesIO()), []]
        with self.assertRaisesRegex(SyncError, 'no day record'):
            plan_week(self.client, [self.entry], self.zone, self.now)
        self.assertEqual([call.args[0] for call in self.client.request.call_args_list], ['GET', 'GET'])

    def test_week_selection(self):
        self.assertEqual(week_start('next', date(2026, 11, 15)), date(2026, 11, 16))
        self.assertEqual(week_start('current', date(2026, 11, 15)), date(2026, 11, 9))
        with self.assertRaises(SyncError):
            week_start('2026-11-17', date(2026, 11, 15))

    def test_all_reads_precede_posts_and_nothing_is_read_afterwards(self):
        later = {**self.entry, 'source_uid': 'tr2', 'date': '2026-11-19', 'body': {**self.body, 'startsAt': '2026-11-19T06:00:00', 'endsAt': '2026-11-19T07:00:00'}}
        self.client.request.side_effect = [[], [], {}, {}]
        actions = plan_week(self.client, [self.entry, later], self.zone, self.now)
        with patch('sync_week.datetime') as clock, redirect_stdout(io.StringIO()):
            clock.now.return_value = self.now
            self.assertEqual(execute_week(self.client, actions, self.zone)['submitted'], 2)
        self.assertEqual([call.args[0] for call in self.client.request.call_args_list], ['GET', 'GET', 'POST', 'POST'])

    def test_three_rapid_runs_can_duplicate_when_fuelin_still_reads_empty(self):
        self.client.request.side_effect = [[], {}, [], {}, [], {}]
        with patch('sync_week.datetime') as clock, redirect_stdout(io.StringIO()):
            clock.now.return_value = self.now
            for _ in range(3):
                execute_week(self.client, plan_week(self.client, [self.entry], self.zone, self.now), self.zone)
        self.assertEqual(sum(call.args[0] == 'POST' for call in self.client.request.call_args_list), 3)

    def test_visible_existing_workout_is_skipped_without_history(self):
        self.client.request.return_value = [{'id': 'visible', **self.body}]
        actions = plan_week(self.client, [self.entry], self.zone, self.now)
        self.assertEqual(actions[0]['action'], 'matched')
        execute_week(self.client, actions, self.zone)
        self.assertEqual(self.client.request.call_count, 1)

    def test_ambiguous_matches_and_source_overlaps_block_all_writes(self):
        self.client.request.return_value = []
        entries = [self.entry, {**self.entry, 'source_uid': 'other'}]
        actions = plan_week(self.client, entries, self.zone, self.now)
        self.assertEqual([item['action'] for item in actions], ['review', 'review'])
        with self.assertRaises(SyncError):
            execute_week(self.client, actions, self.zone)
        self.assertTrue(all(call.args[0] == 'GET' for call in self.client.request.call_args_list))

    def test_one_remote_activity_cannot_match_two_source_sessions(self):
        self.client.request.return_value = [{'id': 'visible', **self.body}]
        actions = plan_week(self.client, [self.entry, {**self.entry, 'source_uid': 'other'}], self.zone, self.now)
        self.assertEqual([item['action'] for item in actions], ['matched', 'review'])

    def test_write_error_stops_without_retry_or_later_posts(self):
        self.client.request.side_effect = TimeoutError()
        actions = [{'action': 'create', 'entry': self.entry, 'snapshot': []}] * 2
        with patch('sync_week.datetime') as clock, redirect_stdout(io.StringIO()), self.assertRaises(TimeoutError):
            clock.now.return_value = self.now
            execute_week(self.client, actions, self.zone)
        self.assertEqual(self.client.request.call_count, 1)

    def test_main_defers_closed_dates_without_creating_local_state(self):
        sample = Path('examples/sample.txt').read_text().replace('over-under intervals.', 'over-under cycling intervals at FTP.')
        distant = sample.replace('20261117', '20270302').replace('example-vennacher', 'far-future')
        self.client.login.return_value = {'timezone': 'Australia/Sydney'}
        for open_days in [[], ['2026-11-17'], ['2026-11-17', '2027-03-02']]:
            with self.subTest(open_days=open_days), tempfile.TemporaryDirectory() as directory:
                self.client.reset_mock()
                pages = [[{'date': day} for day in open_days], []] if open_days else [[]]
                self.client.request.side_effect = pages + [[] for _ in open_days] + [{} for _ in open_days]
                path = Path(directory) / 'feed.ics'
                path.write_text(sample.replace('END:VCALENDAR', 'BEGIN:VEVENT' + distant.split('BEGIN:VEVENT', 1)[1]))
                config = Path(directory) / 'config.json'
                config.write_text(json.dumps({'timezone': 'Australia/Sydney'}))
                before = set(Path(directory).iterdir())
                with patch('sys.argv', ['sync.py', '--file', str(path), '--config', str(config), '--apply']), patch('sync_week.datetime') as clock, patch('sync_week.FuelinClient', return_value=self.client), redirect_stdout(io.StringIO()) as output:
                    clock.now.return_value = self.now
                    self.assertEqual(main(), 0, output.getvalue())
                self.assertEqual(set(Path(directory).iterdir()), before)
                calls = self.client.request.call_args_list
                self.assertEqual([call.args[0] for call in calls], ['GET'] * (len(pages) + len(open_days)) + ['POST'] * len(open_days))
