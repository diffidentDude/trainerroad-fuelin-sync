# TrainerRoad → Fuelin

Sync planned TrainerRoad workouts into Fuelin so its nutrition plan reflects your training. Requires **macOS and Python 3.11+**; no third-party Python packages.

## How it works

The script reads your TrainerRoad calendar feed, discovers which dates Fuelin currently allows you to plan, and reads the existing activities for those dates. It then creates missing workouts and updates uniquely matched activities to match TrainerRoad. Later dates are deferred until Fuelin opens them.

TrainerRoad is the source of truth. Workout length, start time, activity type and intensity are synced. Requests are sent once, with no post-write polling. A local SQLite file tracks submissions and activity IDs to avoid duplicates; later runs can confirm submissions during their initial read.

## Setup

Run commands from this project's directory. Create your local preferences:

```sh
cp config.example.json config.json
```

Edit `config.json`: set `timezone` to your Fuelin account's IANA timezone, and set `start_times.weekday`, `start_times.weekend` and `start_times.gym` using `HH:MM` (24-hour time). The published example uses neutral UTC/09:00 values. Your local config is ignored by Git. Use `--config path/to/file.json` to select another file; keep custom configs outside the repository or use the ignored `config.local.json` filename.

Get your private calendar subscription URL from [TrainerRoad calendar sync](https://www.trainerroad.com/profile/calendar-sync). In zsh, enter it without displaying it or putting the URL in shell history:

```zsh
read -rs 'TRAINERROAD_CALENDAR_URL?Paste calendar URL: '
export TRAINERROAD_CALENDAR_URL
echo
```

Sign in to Fuelin:

```sh
python3 auth.py login
```

Your email, token and account ID are stored in macOS Keychain. Your password is not saved. Sign in again when the session expires. The calendar environment variable must be set in each new terminal session.

## Usage

```sh
python3 sync.py          # Preview; does not change Fuelin activities
python3 sync.py --apply  # Create missing sessions and update existing matches
```

No week parameter is needed. Optional filters include `--week current`, `--week next`, or `--week YYYY-MM-DD` (a Monday). Use `--file path/to/calendar.ics` for a local calendar export, or `--timezone Europe/London` to override the configured timezone; it must match your Fuelin account.

Manage saved authentication with `python3 auth.py status` or `python3 auth.py logout`. Logout removes the local token, not the server session.

## Workout rules

- Missing calendar start times use your configured weekday, weekend or gym time. Explicit TrainerRoad times take precedence. Gym detection uses standard gym/strength/weights words or a title override with one of those activity types.
- Cycling intensity: **IF × 5**, rounded to the nearest integer (halves up), capped at 1–5. Gym defaults to **1**; races without IF default to **5**. This is a chosen conversion, not a validated RPE equivalence.
- Duration comes from the calendar title or structured ICS fields. `config.json` provides title-specific type/intensity overrides, duration fallbacks and recognised annotations. A duration fallback cannot detect a change absent from the feed.
- Keep recurring strength sessions in TrainerRoad only; the sync creates individual Fuelin activities.

## State and limitations

**Keep `sync-state.sqlite3`.** It contains private workout/account metadata and prevents duplicate submissions. Do not delete it to retry an uncertain request. Ambiguous matches, cross-date moves and completed or recurring Fuelin activities requiring changes are flagged for review. Deletions and automatic scheduling are not implemented. Fuelin may display generic workout names because its create endpoint does not accept a name.

This uses Fuelin's documented app endpoints; changes to their behaviour may require updates. Creation and login have been exercised live; PATCH updates have automated coverage but still need live validation.

Calendar URLs, tokens, exports, previews and the database are private. Git ignores local config, exports, state, editor files and the original Swagger download. Credentials stay in Keychain. Only minimal request contracts are included in `fuelin-schema.json`.

## Development

```sh
python3 -m unittest discover -s tests -v
```

Tests use synthetic fixtures and mocked Fuelin calls; they require no accounts or network access. GitHub Actions runs the same suite. `examples/sample.txt` is a synthetic calendar fixture.
