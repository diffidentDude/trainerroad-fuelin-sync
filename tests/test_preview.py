import unittest
import tempfile
from datetime import date
from pathlib import Path

from preview import preview, scheduled_start, read_calendar


class CalendarTests(unittest.TestCase):
    def test_structured_duration_beats_fallback_for_prefixless_workout(self):
        text = self.sample.replace("1:15 - Vennacher -2", "Galena -2").replace("SUMMARY:", "DURATION:PT1H30M\nSUMMARY:")
        result = preview(text, date(2026, 11, 15), "Australia/Sydney", {"Galena -2": 60})
        self.assertEqual(result["workouts"][0]["duration_minutes"], 90)
        self.assertEqual(result["workouts"][0]["duration_source"], "ics_duration")

    def test_timed_end_supplies_duration_without_title_prefix(self):
        text = self.sample.replace("1:15 - Vennacher -2", "Galena -2").replace("DTSTART;VALUE=DATE:20261117", "DTSTART:20261116T190000Z\nDTEND:20261116T203000Z")
        result = preview(text, date(2026, 11, 15), "Australia/Sydney", {"Galena -2": 60})
        self.assertEqual(result["workouts"][0]["duration_minutes"], 90)
        self.assertEqual(result["workouts"][0]["duration_source"], "ics_end")

    def test_all_day_end_does_not_turn_annotation_into_24_hour_workout(self):
        text = self.sample.replace("1:15 - Vennacher -2", "Base 2").replace("SUMMARY:", "DTEND;VALUE=DATE:20261118\nSUMMARY:")
        result = preview(text, date(2026, 11, 15), "Australia/Sydney")
        self.assertEqual(result["workouts"], [])
        self.assertEqual(len(result["skipped_events"]), 1)
    def test_all_future_scope_includes_workouts_beyond_two_weeks(self):
        far_future = self.sample.replace("20261117", "20270302").replace("example-vennacher", "far-future")
        past = self.sample.replace("20261117", "20261114").replace("example-vennacher", "past")
        result = preview(self.sample + far_future + past, date(2026, 11, 15), "Australia/Sydney", future_only=True)
        self.assertEqual([workout["date"] for workout in result["workouts"]], ["2026-11-17", "2027-03-02"])
        self.assertEqual(result["window_start"], "2026-11-15")
        self.assertIsNone(result["window_end_exclusive"])

    def test_explicit_duration_fallback_handles_missing_title_prefix(self):
        text = self.sample.replace("1:15 - Vennacher -2", "Galena -2")
        result = preview(text, date(2026, 11, 15), "Australia/Sydney", {"Galena -2": 60})
        self.assertEqual(result["skipped_events"], [])
        self.assertEqual(result["workouts"][0]["duration_minutes"], 60)
        self.assertEqual(result["workouts"][0]["duration_source"], "configured_override")
        self.assertEqual(result["workouts"][0]["title"], "Galena -2")

    def test_feed_duration_takes_precedence_over_fallback(self):
        result = preview(self.sample, date(2026, 11, 15), "Australia/Sydney", {"Vennacher -2": 60})
        self.assertEqual(result["workouts"][0]["duration_minutes"], 75)

    def test_complete_file_is_accepted_and_truncation_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calendar.ics"
            for text, valid in [(self.sample, True),
                                (self.sample.replace("END:VEVENT\n", ""), False),
                                (self.sample.replace("END:VCALENDAR", ""), False)]:
                path.write_text(text)
                if valid:
                    self.assertEqual(read_calendar(path), text)
                else:
                    with self.assertRaises(ValueError):
                        read_calendar(path)

    def test_configurable_start_times(self):
        config = {"start_times": {"weekday": "05:30", "weekend": "10:15", "gym": "14:00"},
                  "titles": {"Session A": {"type": "strength"}}}
        cases = [
            (date(2026, 11, 17), "Ride", "05:30:00+11:00", "weekday"),
            (date(2026, 11, 21), "Ride", "10:15:00+11:00", "weekend"),
            (date(2026, 11, 22), "Race", "10:15:00+11:00", "weekend"),
            (date(2026, 11, 17), "Session A", "14:00:00+11:00", "gym"),
            (date(2026, 11, 22), "Gym", "14:00:00+11:00", "gym"),
            (date(2026, 7, 14), "Ride", "05:30:00+10:00", "weekday"),
        ]
        for day, title, expected, rule in cases:
            with self.subTest(day=day, title=title):
                timestamp, source = scheduled_start(day, title, None, "Australia/Sydney", config)
                self.assertEqual(timestamp, f"{day}T{expected}")
                self.assertEqual(source, rule)

    def test_explicit_start_is_preserved(self):
        text = self.sample.replace("DTSTART;VALUE=DATE:20261117", "DTSTART:20261116T220000Z")
        workout = preview(text, date(2026, 11, 15), "Australia/Sydney")["workouts"][0]
        self.assertEqual(workout["start_time"], "2026-11-17T09:00:00+11:00")
        self.assertEqual(workout["start_time_source"], "calendar")

    def setUp(self):
        self.sample = Path("examples/sample.txt").read_text()

    def test_metrics_and_duration(self):
        result = preview(self.sample, date(2026, 11, 15), "Australia/Sydney")
        workout = result["workouts"][0]
        self.assertEqual(workout["duration_minutes"], 75)
        self.assertEqual(workout["start_time"], "2026-11-17T09:00:00+11:00")
        self.assertEqual(workout["tss"], 89)
        self.assertEqual(workout["intensity_factor"], 0.85)
        self.assertEqual(workout["energy_kj"], 979)
        self.assertIsNone(workout["fuelin_intensity"])
        self.assertEqual(result["window_end_exclusive"], "2026-11-23")

    def test_excludes_following_monday(self):
        text = self.sample.replace("20261117", "20261123")
        self.assertEqual(preview(text, date(2026, 11, 15), "Australia/Sydney")["workouts"], [])

    def test_utc_midnight_boundary(self):
        text = self.sample.replace("DTSTART;VALUE=DATE:20261117", "DTSTART:20261122T140000Z")
        self.assertEqual(preview(text, date(2026, 11, 15), "Australia/Sydney")["workouts"], [])

    def test_folding_and_text_escaping(self):
        text = self.sample.replace("IF 0.85", "IF \n 0.85")
        workout = preview(text, date(2026, 11, 15), "Australia/Sydney")["workouts"][0]
        self.assertEqual(workout["intensity_factor"], 0.85)
        self.assertIn("TSS 89, IF", workout["description"])

    def test_recurrence_requires_support(self):
        with self.assertRaises(ValueError):
            preview(self.sample.replace("SUMMARY:", "RRULE:FREQ=WEEKLY\nSUMMARY:"), date(2026, 11, 15), "Australia/Sydney")

    def test_annotations_are_visible(self):
        result = preview(self.sample.replace("1:15 - Vennacher -2", "Recovery week"), date(2026, 11, 15), "Australia/Sydney")
        self.assertEqual(len(result["skipped_events"]), 1)
        self.assertEqual(result["workouts"], [])

    def test_duplicate_identity_stops_preview(self):
        with self.assertRaises(ValueError):
            preview(self.sample + self.sample, date(2026, 11, 15), "Australia/Sydney")


if __name__ == "__main__":
    unittest.main()
