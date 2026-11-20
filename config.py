"""Load user preferences from an ignored local file."""
import json
import math
import re
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_START_TIMES = {"weekday": "09:00", "weekend": "09:00", "gym": "09:00"}


class ConfigError(ValueError):
    pass


DEFAULT_IF_RANGES = [
    {"level": 1, "min": 0.00, "max": 0.30},
    {"level": 2, "min": 0.30, "max": 0.50},
    {"level": 3, "min": 0.50, "max": 0.75},
    {"level": 4, "min": 0.75, "max": 0.90},
    {"level": 5, "min": 0.90, "max": None},
]


def validate_if_ranges(ranges):
    if not isinstance(ranges, list) or len(ranges) != 5:
        raise ConfigError("rpe.if_ranges must contain five bands, ordered from level 1 to 5")
    previous = 0
    for level, band in enumerate(ranges, 1):
        if (not isinstance(band, dict) or set(band) != {"level", "min", "max"}
                or type(band["level"]) is not int or band["level"] != level):
            raise ConfigError("Each IF band must have level, min and max, in level order 1–5")
        lower, upper = band["min"], band["max"]
        if type(lower) not in (int, float) or not math.isfinite(lower) or lower < 0:
            raise ConfigError("IF min values must be finite and non-negative")
        if lower != previous:
            raise ConfigError("IF ranges must start at 0 and each min must equal the previous max (no gaps or overlaps)")
        if level == 5:
            if upper is not None:
                raise ConfigError("Level 5 max must be null to cover all higher IF values")
        elif type(upper) not in (int, float) or not math.isfinite(upper) or upper <= lower:
            raise ConfigError("Each finite IF max must be greater than its min")
        else:
            previous = upper
    return ranges


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
        raise ConfigError("rpe.if_level_3_max was removed; configure all levels with rpe.if_ranges instead")
    if "if_ranges" in rules.get("rpe", {}):
        validate_if_ranges(rules["rpe"]["if_ranges"])
    for key in ("titles", "workouts"):
        if any(not isinstance(value, dict) for value in rules.get(key, {}).values()):
            raise ConfigError(f"Each {key} entry must be an object")
    if not isinstance(rules.get("annotations", []), list) or any(not isinstance(value, str) for value in rules.get("annotations", [])):
        raise ConfigError("annotations must be a list of titles")
    return rules
