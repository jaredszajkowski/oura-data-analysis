# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

This is **not a code project** — it is a personal data export from the Oura ring app. There is no build system, no dependencies, and no tests. Work here means reading, parsing, cleaning, joining, and analyzing the exported files (typically with ad-hoc Python/pandas or shell). Any analysis scripts or notebooks are things you create as needed; none ship with the repo.

## Layout

- `data.zip` — the original, unmodified Oura export archive. `data/` is its extracted contents. Treat `data.zip` as the source of truth; if `data/` looks corrupted or partial, re-extract with `unzip -o data.zip`.
- `data/App Data/` — 61 CSV files, one per Oura data type (sleep, readiness, activity, heart rate, workouts, temperature, etc.). This is where nearly all analysis happens.
- `data/Subscriptions/` — billing/account records: `account.csv`, `contact.csv`, `payment-method.csv`, and per-invoice PDFs. Rarely relevant to health analysis.

## CSV format quirks (important)

These files do not behave like typical CSVs. Verify format before parsing:

- **Delimiter is `;` (semicolon), not comma.** Use `pd.read_csv(path, sep=';')` or `csv.reader(..., delimiter=';')`.
- **Several columns contain embedded JSON** as quoted strings — notably the `contributors` column in `dailysleep.csv`, `dailyreadiness.csv`, and `dailyactivity.csv` (e.g. `{"deep_sleep": 85, "efficiency": 90, ...}`). Parse these with `json.loads` after reading; do not try to split on them.
- **`null` appears inside the JSON** and empty strings appear as bare `;;` for missing numeric fields — expect NaN/None and guard for it.
- **~12 of the 61 CSVs are effectively empty** (header only or near-zero bytes) because the user doesn't use those features (e.g. blood glucose, medications, contraception). Don't treat an empty file as an error.
- **File sizes vary enormously.** High-frequency streams are large — `temperature.csv` (~680k rows), `heartrate.csv` (~574k rows), `stepcount.csv` (~357k rows). Read these with chunking/`usecols` rather than loading whole. Daily-summary files (`dailysleep`, `dailyreadiness`, `dailyactivity`) are ~465–480 rows and cheap.

## Key files and how they join

- **Daily summaries** (`dailysleep.csv`, `dailyreadiness.csv`, `dailyactivity.csv`) have one row per calendar day with a `day` column (`YYYY-MM-DD`) and a `score`. Join these to each other on `day`.
- **Timestamped event/stream files** (`heartrate.csv`, `temperature.csv`, `stepcount.csv`, `workout.csv`, `session.csv`) use ISO-8601 `timestamp`/`start_datetime` columns, sometimes UTC (`Z`) and sometimes with an offset (`-05:00`) — normalize timezones before comparing or joining to daily data. To align a stream to a daily summary, derive a `day` from the timestamp (respecting the local offset).
- Current data spans roughly **2025-04 through the export date**. The export is a full historical dump, not incremental.

## Conventions

- The user's data is private health information. Keep analysis local; do not upload raw data or PDFs to external services.
- Prefer non-destructive work: read from `data/`, write outputs (cleaned frames, charts, reports) to new files rather than overwriting the export. If `data/` is ever damaged, re-extract from `data.zip`.
