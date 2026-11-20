# TrainerRoad → Fuelin

Sync planned TrainerRoad workouts into Fuelin so its nutrition plan reflects your training. Requires **macOS and Python 3.11+**; no third-party Python packages.

## How it works

The script reads your TrainerRoad calendar feed, discovers which dates Fuelin currently allows you to plan, and reads the existing activities for those dates. It then creates missing workouts and updates uniquely matched activities to match TrainerRoad. Later dates are deferred until Fuelin opens them.

TrainerRoad is the source of truth. Workout length, start time, activity type and intensity are synced. Requests are sent once, with no post-write polling. There is no local workout database or submission history: matching uses only the Fuelin snapshot read at the start of each run.

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
- Cycling intensity uses `rpe.if_ranges`, with a `level`, `min` and `max` for every band. Ranges must start at 0 and meet exactly (each `min` equals the previous `max`). Maximums are inclusive; at a shared endpoint the **lower level wins**, so IF 0.75 maps to level 3. Zero is included in level 1, and level 5 uses `"max": null` for no upper limit. See the full editable ranges in `config.example.json`. This is a configurable conversion, not a validated RPE equivalence.
- Gym defaults to **1** and races without IF to **5**, configured separately under `rpe`. Explicit title/workout intensity overrides take precedence over the IF ranges.
- For known cycling activities without IF, the sync estimates it as `sqrt(TSS / (duration_hours × 100))` when TSS and duration are present. The estimate is shown in the report and mapped using your IF ranges. Supplied IF always takes precedence. This assumes power-based cycling TSS; it is not applied to unknown sports, running or strength. Title rules such as `"Club event": {"type": "bike"}` identify cycling events whose names/descriptions lack sport keywords. See [TrainingPeaks' TSS formula](https://www.trainingpeaks.com/learn/articles/how-to-plan-your-season-with-training-stress-score/).
- Duration comes from the calendar title or structured ICS fields. `config.json` provides title-specific type/intensity overrides, duration fallbacks and recognised annotations. A duration fallback cannot detect a change absent from the feed.
- Keep recurring strength sessions in TrainerRoad only; the sync creates individual Fuelin activities.
- A duration-less `Rest Day` marker means no training is planned on that date. Set `rest_day_action` in your local config to `delete_all_planned` to remove all uncompleted Fuelin workouts for that date, including strength. Completed workouts are kept. The alternatives are `delete_cycling` or `review` (the default). Rest-day reads and planned deletions appear in the preview; deletions require `--apply` and have no post-write verification. Other non-workout titles can be listed in `annotations`. Unknown entries on open Fuelin dates block writes for review.

## Limitations

- **Rapid or concurrent runs can create duplicates.** Fuelin may take time to expose a submitted workout. If you run the sync three times before those writes become visible, each run may see the same empty state and submit the workout again. Wait until the activities appear in Fuelin before rerunning. The tool has no local history, idempotency guarantee, polling or write retries.
- A timeout or failed request can still have reached Fuelin. Check the app before repeating a run; earlier requests in that run may already have succeeded.
- Matching uses date, sport and times, not persistent TrainerRoad-to-Fuelin IDs. Ambiguous matches and completed or recurring activities requiring changes are flagged for review. Cross-date moves, or changing both sport and time, may leave an old activity unmatched; resolve those manually. Workouts removed from TrainerRoad without an explicit Rest Day marker are not automatically deleted. Automatic scheduling is not implemented.
- Fuelin may display generic workout names because its create endpoint does not accept a name.

Legacy `sync-state.sqlite3` files are no longer read or written and can be deleted. Authentication tokens still persist in macOS Keychain; workout state does not.

This uses Fuelin's documented app endpoints; changes to their behaviour may require updates. Creation and login have been exercised live; PATCH updates have automated coverage but still need live validation.

Calendar URLs, tokens, exports and previews are private. Git ignores local config, exports, legacy database files, editor files and the original Swagger download. Credentials stay in Keychain. Only minimal request contracts are included in `fuelin-schema.json`.

## Development

```sh
python3 -m unittest discover -s tests -v
```

Tests use synthetic fixtures and mocked Fuelin calls; they require no accounts or network access. GitHub Actions runs the same suite. `examples/sample.txt` is a synthetic calendar fixture.
