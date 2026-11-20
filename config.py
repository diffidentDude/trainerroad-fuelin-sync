"""Load user preferences from an ignored local file."""
import json
import math
import re
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_START_TIMES = {"weekday": "09:00", "weekend": "09:00", "gym": "09:00"}


class ConfigError(ValueError):
    pass


def validate_if_level_3_max(value):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or not 0.5 <= value < 0.9):
        raise ConfigError("rpe.if_level_3_max must be a decimal IF from 0.50 up to (but not including) 0.90")
    return value


def load_config(path=Path("config.json")):
    path = Path(path)
    if not path.exists():
        raise ConfigError("Config missing: copy config.example.json to config.json and edit your preferences")
    try:
        rules = json.loads(path.read_text())
    except json.JSONDecodeError:
        raise ConfigError("Config is not valid JSON") from None
    if not isinstance(rules, dict):
        raise ConfigError("Config must be a JSON object")
    try:
        ZoneInfo(rules.get("timezone", "UTC"))
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        raise ConfigError("Config timezone must be a valid IANA timezone") from None
    if rules.get("rest_day_action", "review") not in ("review", "delete_all_planned", "delete_cycling"):
        raise ConfigError("rest_day_action must be review, delete_all_planned or delete_cycling")
    times = rules.get("start_times", {})
    if not isinstance(times, dict):
        raise ConfigError("start_times must be an object")
    if set(times) - set(DEFAULT_START_TIMES):
        raise ConfigError("Start-time keys must be weekday, weekend or gym")
    for value in times.values():
        if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ConfigError("Start times must use HH:MM (24-hour time)")
    for key in ("rpe", "titles", "workouts"):
        if not isinstance(rules.get(key, {}), dict):
            raise ConfigError(f"{key} must be an object")
    if "if_level_3_max" in rules.get("rpe", {}):
        validate_if_level_3_max(rules["rpe"]["if_level_3_max"])
    for key in ("titles", "workouts"):
        if any(not isinstance(value, dict) for value in rules.get(key, {}).values()):
            raise ConfigError(f"Each {key} entry must be an object")
    if not isinstance(rules.get("annotations", []), list) or any(not isinstance(value, str) for value in rules.get("annotations", [])):
        raise ConfigError("annotations must be a list of titles")
    return rules
