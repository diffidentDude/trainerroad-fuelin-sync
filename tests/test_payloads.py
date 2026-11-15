import json
import unittest
from pathlib import Path

from payloads import build_plan, payload, classify, intensity_from_if


class PayloadTests(unittest.TestCase):
    def test_linear_if_mapping(self):
        for value, expected in [(0, 1), (.2, 1), (.4, 2), (.6, 3), (.69, 3),
                                (.7, 4), (.71, 4), (.8, 4), (.85, 4), (.86, 4),
                                (.9, 5), (1, 5), (1.2, 5)]:
            with self.subTest(value=value):
                self.assertEqual(intensity_from_if(value), expected)
        for value in [True, -1, float('nan'), float('inf'), "0.8", None]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                intensity_from_if(value)

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
