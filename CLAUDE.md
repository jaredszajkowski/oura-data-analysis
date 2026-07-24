# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Scripts and notes for analyzing a personal Oura ring data export. The repo tracks
**only code and documentation** — `src/extract_session_create_gpx.py`, `README.md`,
this file, `requirements.txt`, `LICENSE`, and `.gitignore`. The raw export is private
health data and is deliberately **kept local and gitignored** (`data/`, `data.zip`,
`sessions/`, plus `*.csv`/`*.gpx`/`*.pdf`/`*.parquet`). Never commit data files.

There is no build system, no dependencies, and no test suite. The script is
**stdlib-only** (Python 3) — `requirements.txt` lists nothing; no venv or `pip install`
needed. Any further analysis is ad-hoc Python/pandas or shell that you create as needed.

## The data (local, not committed)

- `data.zip` — the original, unmodified Oura export archive. `data/` is its extracted
  contents. Treat `data.zip` as the source of truth; if `data/` looks corrupted or
  partial, re-extract with `unzip -o data.zip`.
- `data/App Data/` — 61 CSV files, one per Oura data type (sleep, readiness, activity,
  heart rate, workouts, temperature, etc.). This is where nearly all analysis happens.
- `data/Subscriptions/` — billing/account records (`account.csv`, `contact.csv`,
  `payment-method.csv`, per-invoice PDFs). Rarely relevant to health analysis.
- Data spans roughly **2025-04 through the export date** (~2026-07). Full historical
  dump, not incremental.

## src/extract_session_create_gpx.py

Extracts everything recorded for one Oura workout and builds an enriched GPS track.
Run it **from the repo root** (it derives `data/` from its own location via `REPO_ROOT
= dirname(dirname(__file__))`, so it works regardless of cwd):

```bash
python3 src/extract_session_create_gpx.py <workout_id> [--outdir <dir>]
```

`<workout_id>` is a UUID from `data/App Data/workout.csv` (run with a bad id and it
prints example ids). Output defaults to `<repo>/sessions/`; for each workout it writes
`sessions/<id>/<id>.{gpx,json,md}`:

- **`.gpx`** — a GPX 1.1 track from `rawlocation.csv` (the only file with GPS), with
  nearest-in-time heart rate, cadence, and skin temperature attached as Garmin
  `TrackPointExtension` tags.
- **`.json` / `.md`** — sidecars: a session summary plus, for every other CSV, the rows
  that match the workout (numeric min/max/mean, time bounds, and rows when few enough).

Non-obvious behavior worth knowing before editing it:

- **No id links the sensor streams to a workout.** Only `workout.csv` carries the id, so
  streams are joined to the `[start_datetime, end_datetime]` window **by time**. The
  scanner picks a strategy per file: id-match → point-in-window (`timestamp`) →
  interval-overlap (`start/end` pairs) → day-match (`day`). A file with both `day` and
  `timestamp` is treated as a daily summary and matched on `day`.
- **GPS only exists from ~2025-04-16 onward** (when `rawlocation.csv` begins), and indoor
  workouts have none. With no points it skips the GPX and warns, but still writes sidecars.
- **Cadence is derived** steps/min from `stepcount.csv` (Oura records no true cadence),
  and is only emitted for gait activities (walking/running/hiking/…) — steps are noise
  for cycling etc. `atemp` in the GPX is **skin** temperature, not ambient.
- **Average speed/pace is distance ÷ duration** (as the Oura app reports it), not the
  mean of instantaneous GPS speed samples.

## CSV format quirks (important)

These files do not behave like typical CSVs. Verify format before parsing:

- **Delimiter is `;` (semicolon), not comma.** Use `pd.read_csv(path, sep=';')` or
  `csv.reader(..., delimiter=';')`.
- **Several columns contain embedded JSON** as quoted strings — notably the `contributors`
  column in `dailysleep.csv`, `dailyreadiness.csv`, and `dailyactivity.csv` (e.g.
  `{"deep_sleep": 85, "efficiency": 90, ...}`). Parse with `json.loads` after reading; do
  not split on them. These blobs are wide — raise `csv.field_size_limit` if using `csv`.
- **`null` appears inside the JSON** and empty strings appear as bare `;;` for missing
  numeric fields — expect NaN/None and guard for it.
- **Timestamps are mixed.** ISO-8601 `timestamp`/`start_datetime` columns are sometimes
  UTC (`Z`) and sometimes offset (`-05:00`) — `workout.csv` uses local offsets while the
  streams use `Z`. Normalize to UTC before comparing or joining. **Exception:**
  `stepcount.csv`'s `end_time` is **Unix epoch milliseconds**, not ISO.
- **~12 of the 61 CSVs are effectively empty** (header only) because the user doesn't use
  those features (blood glucose, medications, contraception). Don't treat empty as an error.
- **File sizes vary enormously.** High-frequency streams are large — `temperature.csv`
  (~680k rows, cols `timestamp;skin_temp`), `heartrate.csv` (~574k rows, `timestamp;bpm;
  source`), `stepcount.csv` (~357k rows). Read these with chunking/`usecols` rather than
  whole. Daily-summary files are ~465–480 rows and cheap.

## Key daily files and how they join

**Daily summaries** (`dailysleep.csv`, `dailyreadiness.csv`, `dailyactivity.csv`) have one
row per calendar day with a `day` column (`YYYY-MM-DD`) and a `score`. Join these to each
other on `day`. To align a timestamped stream to a daily summary, derive a `day` from the
timestamp (respecting the local offset).

## Conventions

- Keep analysis local; do not upload raw data or PDFs to external services.
- Prefer non-destructive work: read from `data/`, write outputs (cleaned frames, charts,
  reports, `sessions/`) to new files rather than overwriting the export. If `data/` is ever
  damaged, re-extract from `data.zip`.
