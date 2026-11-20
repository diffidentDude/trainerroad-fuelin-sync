import json
import unittest
from pathlib import Path

from payloads import build_plan, payload, classify, intensity_from_if, estimate_if
from preview import preview as calendar_preview
from datetime import date


class PayloadTests(unittest.TestCase):
    def test_tss_and_duration_estimate_cycling_if(self):
        self.assertAlmostEqual(estimate_if(92, 55), 1.0018165319240662)
        self.assertEqual(estimate_if(100, 60), 1)
        self.assertEqual(estimate_if(0, 60), 0)
        for tss, minutes in [(-1, 55), (True, 55), (92, 0), (92, -1),
                             (float('nan'), 55), (92, float('inf')), ('92', 55)]:
            with self.subTest(tss=tss, minutes=minutes), self.assertRaises(ValueError):
                estimate_if(tss, minutes)

    def test_tss_only_event_flows_from_calendar_to_level_five_payload(self):
        ics = ('BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:event\nDTSTART;VALUE=DATE:20270328\n'
               'SUMMARY:0:55 - Club event\nDESCRIPTION:TSS 92. Description: Club event\n'
               'END:VEVENT\nEND:VCALENDAR\n')
        source = calendar_preview(ics, date(2027, 3, 22), 'UTC')
        schema = json.loads(Path('fuelin-schema.json').read_text())
        entry = build_plan(source, {'titles': {'Club event': {'type': 'bike'}}}, schema)['entries'][0]
        self.assertEqual(entry['classification'], 'tss_duration')
        self.assertAlmostEqual(entry['estimated_if'], 1.0018165319240662)
        self.assertEqual(entry['body']['intensity'], 5)
        self.assertEqual(set(entry['body']), {'type', 'intensity', 'startsAt', 'endsAt'})

    def test_supplied_if_wins_and_other_sports_are_not_estimated(self):
        base = {'title': 'Event', 'tss': 92, 'duration_minutes': 55}
        settings, source = classify({**base, 'activity_type': 'bike', 'intensity_factor': .6}, {})
        self.assertEqual(settings['intensity'], 3)
        self.assertEqual(source, 'intensity_factor')
        for sport in [None, 'run', 'swim']:
            with self.subTest(sport=sport):
                settings, _ = classify({**base, 'activity_type': sport}, {})
                self.assertNotIn('estimated_if', settings)
                self.assertNotIn('intensity', settings)
    def test_default_if_bands_include_their_upper_boundary(self):
        for value, expected in [(0, 1), (.3, 1), (.300001, 2), (.5, 2), (.500001, 3),
                                (.7, 3), (.75, 3), (.750001, 4), (.9, 4), (.900001, 5), (1, 5), (1.2, 5)]:
            with self.subTest(value=value):
                self.assertEqual(intensity_from_if(value), expected)
        for value in [True, -1, float('nan'), float('inf'), "0.8", None]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                intensity_from_if(value)

    def test_custom_bands_reach_planned_payload_and_overrides_still_win(self):
        bands = [{"level": level, "min": lower, "max": upper} for level, (lower, upper) in enumerate(zip([0, .4, .6, .8, 1.0], [.4, .6, .8, 1.0, None]), 1)]
        for value, expected in [(.35, 1), (.55, 2), (.75, 3), (.95, 4), (1.1, 5)]:
            self.assertEqual(intensity_from_if(value, bands), expected)
        schema = json.loads(Path("fuelin-schema.json").read_text())
        workout = {**self.workout, "intensity_factor": .8}
        preview = {"timezone": "UTC", "workouts": [workout]}
        config = {"rpe": {"if_ranges": bands}}
        self.assertEqual(build_plan(preview, config, schema)["entries"][0]["body"]["intensity"], 3)
        config["workouts"] = {"test": {"intensity": 5}}
        self.assertEqual(build_plan(preview, config, schema)["entries"][0]["body"]["intensity"], 5)

    def test_if_takes_precedence_over_description_and_race_default(self):
        for title in ["Ride", "Bike race"]:
            settings, reason = classify({"title": title, "description": "Hard threshold ride",
                                         "intensity_factor": .6}, {})
            self.assertEqual(settings, {"type": "bike", "intensity": 3})
            self.assertEqual(reason, "intensity_factor")

    def test_gym_and_race_without_if(self):
        self.assertEqual(classify({"title": "Gym session"}, {})[0], {"type": "gym", "intensity": 1})
        self.assertEqual(classify({"title": "Bike race"}, {})[0], {"type": "bike", "intensity": 5})
        settings, _ = classify({"title": "Ride", "description": "Sweet Spot"}, {})
        self.assertNotIn("intensity", settings)

    def test_invalid_if_does_not_fall_back_to_race_intensity(self):
        settings, reason = classify({"title": "Bike race", "intensity_factor": -1}, {})
        self.assertNotIn("intensity", settings)
        self.assertEqual(reason, "invalid_intensity_factor")

    def setUp(self):
        self.workout = {"source_uid": "test", "title": "Ride", "date": "2026-11-17",
                        "duration_minutes": 75, "start_time": "2026-11-17T06:00:00+11:00"}
        self.types = ["bike", "gym"]

    def test_emits_fuelin_local_wall_times_and_derives_end(self):
        result = payload(self.workout, {"type": "bike", "intensity": 3}, self.types)
        self.assertEqual(result, {"type": "bike", "intensity": 3,
                                "startsAt": "2026-11-17T06:00:00", "endsAt": "2026-11-17T07:15:00"})

    def test_no_intensity_guess(self):
        for intensity in [None, 0, 6, True, "3", 2.5]:
            with self.subTest(intensity=intensity), self.assertRaises(ValueError):
                payload(self.workout, {"type": "bike", "intensity": intensity}, self.types)

    def test_invalid_duration_or_naive_time(self):
        for changes in [{"duration_minutes": -1}, {"duration_minutes": float('nan')},
                        {"start_time": "2026-11-17T06:00:00"}]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                payload({**self.workout, **changes}, {"type": "bike", "intensity": 3}, self.types)

    def test_uid_override_and_schema_contract(self):
        schema = json.loads(Path("fuelin-schema.json").read_text())
        config = {"titles": {"Ride": {"type": "bike", "intensity": 2}},
                  "workouts": {"test": {"intensity": 4}}}
        plan = build_plan({"timezone": "Australia/Sydney", "workouts": [self.workout]}, config, schema)
        entry = plan["entries"][0]
        self.assertEqual(entry["date"], "2026-11-17")  # Route and payload both use local date.
        self.assertEqual(entry["body"]["intensity"], 4)
        contract = schema["components"]["schemas"]["InsertWorkout"]
        self.assertEqual(set(entry["body"]), set(contract["required"]))
        self.assertTrue(set(entry["body"]) <= set(contract["properties"]))

    def test_unresolved_workout_has_no_sendable_body(self):
        schema = json.loads(Path("fuelin-schema.json").read_text())
        entry = build_plan({"timezone": "Australia/Sydney", "workouts": [self.workout]}, {}, schema)["entries"][0]
        self.assertEqual(entry["status"], "needs_review")
        self.assertNotIn("body", entry)
