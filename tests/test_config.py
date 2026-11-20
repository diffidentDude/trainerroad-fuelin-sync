import json
from copy import deepcopy
import tempfile
import unittest
from pathlib import Path

from config import ConfigError, load_config, DEFAULT_IF_RANGES, validate_if_ranges


class ConfigTests(unittest.TestCase):
    def test_example_is_valid_and_has_no_workout_specific_preferences(self):
        rules = load_config("config.example.json")
        self.assertEqual(rules["timezone"], "UTC")
        self.assertEqual(rules["titles"], {})
        self.assertEqual(rules["workouts"], {})
        self.assertEqual(set(rules["start_times"].values()), {"09:00"})

    def test_example_shows_all_five_bands(self):
        self.assertEqual(load_config("config.example.json")["rpe"]["if_ranges"], DEFAULT_IF_RANGES)

    def test_incomplete_overlapping_unordered_or_unbounded_profiles_fail(self):
        bad = [[], {}, DEFAULT_IF_RANGES[:-1]]
        for index, field, value in [(0, "max", -.1), (0, "max", True),
                                    (1, "max", .3), (1, "max", float('nan')),
                                    (2, "max", None), (3, "max", float('inf')),
                                    (4, "max", 1.2), (2, "level", 4), (0, "level", True),
                                    (0, "min", .1), (1, "min", .31), (1, "min", .29),
                                    (2, "min", None), (2, "min", True), (4, "min", float("inf"))]:
            bands = deepcopy(DEFAULT_IF_RANGES)
            bands[index][field] = value
            bad.append(bands)
        for bands in bad:
            with self.subTest(bands=bands), self.assertRaises(ConfigError):
                validate_if_ranges(bands)

    def test_missing_config_explains_setup(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ConfigError, "copy config.example.json"):
                load_config(Path(directory) / "missing.json")

    def test_invalid_times_timezone_and_rule_shapes_fail_early(self):
        invalid = [
            {"rest_day_action": "delete_everything"},
            {"rpe": {"if_level_3_max": .75}},
            {"rpe": {"if_ranges": []}},
            {"start_times": {"weekday": "25:00"}},
            {"start_times": {"weekday": "6:00"}},
            {"start_times": {"weekdays": "09:00"}},
            {"timezone": "Not/A_Timezone"},
            {"titles": {"Session A": "strength"}},
            {"annotations": "not a list"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            for rules in invalid:
                path.write_text(json.dumps(rules))
                with self.subTest(rules=rules), self.assertRaises(ConfigError):
                    load_config(path)
