# Oura Data Analysis

Scripts and notes for working with a personal [Oura ring](https://ouraring.com/) data export.

> **Note:** This repository contains **only code and documentation**. The raw
> Oura export (health CSVs, billing PDFs, and GPS workout tracks) is private
> health data and is intentionally **kept local** — it is excluded via
> `.gitignore` and never committed here.

## Contents

- `extract_session.py` — parse and extract individual Oura workout/session data.
- `CLAUDE.md` — working notes on the export's layout and CSV format quirks
  (semicolon-delimited, embedded JSON columns, large high-frequency streams, etc.).

## Data layout (kept local, not in this repo)

- `data/App Data/` — one CSV per Oura data type (sleep, readiness, activity,
  heart rate, temperature, workouts, …).
- `data/Subscriptions/` — billing/account records.
- `data.zip` — the original, unmodified Oura export archive.
