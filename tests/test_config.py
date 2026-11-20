import json
import tempfile
import unittest
from pathlib import Path

from config import ConfigError, load_config


class ConfigTests(unittest.TestCase):
    def test_example_is_valid_and_has_no_workout_specific_preferences(self):
        rules = load_config("config.example.json")
        self.assertEqual(rules["timezone"], "UTC")
        self.assertEqual(rules["titles"], {})
        self.assertEqual(rules["workouts"], {})
        self.assertEqual(set(rules["start_times"].values()), {"09:00"})

    def test_missing_config_explains_setup(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ConfigError, "copy config.example.json"):
                load_config(Path(directory) / "missing.json")

    def test_invalid_times_timezone_and_rule_shapes_fail_early(self):
        invalid = [
            {"rest_day_action": "delete_everything"},
            {"rpe": {"if_level_3_max": 75}},
            {"rpe": {"if_level_3_max": True}},
            {"rpe": {"if_level_3_max": .49}},
            {"rpe": {"if_level_3_max": .9}},
            {"rpe": {"if_level_3_max": "0.75"}},
            {"rpe": {"if_level_3_max": float("nan")}},
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
