# Oura Data Analysis

Scripts and notes for working with a personal [Oura ring](https://ouraring.com/) data export.

> **Note:** This repository contains **only code and documentation**. The raw
> Oura export (health CSVs, billing PDFs, and GPS workout tracks) is private
> health data and is intentionally **kept local** — it is excluded via
> `.gitignore` and never committed here.

## Contents

- `src/extract_session_create_gpx.py` — parse and extract individual Oura workout/session data
  (see [Usage](#usage)).
- `requirements.txt` — dependency manifest (none: `extract_session_create_gpx.py` is Python 3 stdlib-only).
- `CLAUDE.md` — working notes on the export's layout and CSV format quirks
  (semicolon-delimited, embedded JSON columns, large high-frequency streams, etc.).
- `LICENSE` — repository license.

## Data layout (kept local, not in this repo)

- `data/App Data/` — one CSV per Oura data type (sleep, readiness, activity,
  heart rate, temperature, workouts, …).
- `data/Subscriptions/` — billing/account records.
- `data.zip` — the original, unmodified Oura export archive.
- `sessions/` — per-session workout exports written by `extract_session_create_gpx.py`
  (include raw GPS tracks / location data).

## Usage

Requires Python 3 (standard library only — nothing to install) and the export
extracted to `data/` at the repo root.

`src/extract_session_create_gpx.py` pulls together everything Oura recorded for a single
workout and writes a per-session bundle:

```bash
# From the repo root:
python3 src/extract_session_create_gpx.py <workout_id>
```

`<workout_id>` is a UUID from `data/App Data/workout.csv`. If you don't have one
handy, run the script with any invalid id and it prints the total workout count
plus a few example ids to copy from:

```bash
python3 src/extract_session_create_gpx.py bad-id
```

For the given workout the script writes `sessions/<id>/<id>.{gpx,json,md}`:

- **`.gpx`** — a GPX 1.1 track from `rawlocation.csv`, with nearest-in-time
  heart rate, cadence, and skin temperature attached as Garmin
  `TrackPointExtension` tags. Skipped for indoor workouts or those before the
  location stream begins (~2025-04-16); the sidecars are still written.
- **`.json` / `.md`** — a session summary plus, for every other CSV, the rows
  that match the workout (min/max/mean, time bounds, and rows when few enough).

Options:

- `--outdir <dir>` — write session bundles somewhere other than the default
  `<repo>/sessions/`.

Outputs land in `sessions/`, which is gitignored (the tracks contain GPS /
location data) — see [Data layout](#data-layout-kept-local-not-in-this-repo).
