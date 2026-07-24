#!/usr/bin/env python3
"""Extract all data recorded for one Oura workout and build a GPX track.

Given a workout UUID from ``data/App Data/workout.csv``, this script:

  1. Looks up the workout row and its ``[start_datetime, end_datetime]`` window.
  2. Builds an enriched GPX track from ``rawlocation.csv`` (the only file with
     GPS coordinates), attaching nearest-in-time heart rate, cadence and skin
     temperature as Garmin TrackPointExtension tags.
  3. Scans every other CSV in ``data/App Data/`` for rows that match the
     workout -- by id where an id-like column exists, otherwise by time
     (point-in-window / interval-overlap / same day).
  4. Writes ``<id>.gpx``, ``<id>.json`` and ``<id>.md`` under ``sessions/<id>/``.

There is no id linking the sensor streams to a workout, so the join is
time-based; all timestamps are normalized to UTC first because ``workout.csv``
uses local offsets while the streams use UTC ``Z``.

Stdlib only. Usage::

    python3 src/extract_session_create_gpx.py <workout_id> [--outdir sessions]
"""

import argparse
import bisect
import csv
import datetime as dt
import glob
import json
import os
import sys
import xml.sax.saxutils as sax

# csv can hold very wide JSON blobs (the daily "contributors" column); raise the
# field-size limit so reads never blow up.
csv.field_size_limit(10 * 1024 * 1024)

HERE = os.path.dirname(os.path.abspath(__file__))
# The export and outputs live at the repo root; this script sits in src/.
REPO_ROOT = os.path.dirname(HERE)
DATA_DIR = os.path.join(REPO_ROOT, "data", "App Data")
WORKOUT_CSV = os.path.join(DATA_DIR, "workout.csv")

# How close (seconds) a sensor sample must be to a trackpoint to be attached.
ENRICH_TOL_S = 30.0
# Store full rows in the sidecars only when a file matches at most this many.
ROW_CAP = 50
# Cadence is derived from step counts, so it is only meaningful for foot-based
# (gait) activities. For cycling and everything else, steps are noise and pedal
# cadence is not recorded by the ring, so cadence is omitted. Matched
# case-insensitively against workout.csv's `activity`.
CADENCE_ACTIVITIES = frozenset(
    {"walking", "running", "hiking", "jogging", "trailrunning", "walk", "run", "hike"}
)

UTC = dt.timezone.utc


# --------------------------------------------------------------------------- #
# Time helpers (the reused join core)
# --------------------------------------------------------------------------- #
def parse_ts(s):
    """Parse an ISO-8601 timestamp to an aware UTC datetime, or None.

    Handles trailing ``Z`` and numeric offsets. A tz-naive value is assumed to
    already be UTC.
    """
    if s is None:
        return None
    s = s.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return d.astimezone(UTC)


def parse_epoch_ms(s):
    """Parse a Unix epoch-in-milliseconds string to aware UTC datetime, or None.

    Some columns (e.g. stepcount.end_time) store epoch ms rather than ISO.
    """
    if not s:
        return None
    s = s.strip()
    if not s.isdigit():
        return None
    try:
        return dt.datetime.fromtimestamp(int(s) / 1000.0, tz=UTC)
    except (ValueError, OverflowError, OSError):
        return None


def in_window(ts, start, end):
    return ts is not None and start <= ts <= end


def overlaps(a_start, a_end, start, end):
    """True if interval [a_start, a_end] intersects [start, end]."""
    if a_start is None and a_end is None:
        return False
    a_start = a_start or a_end
    a_end = a_end or a_start
    return a_start <= end and a_end >= start


class Series:
    """A time-sorted list of (datetime, value) for nearest-in-time lookups."""

    def __init__(self, pairs):
        pairs = sorted(pairs, key=lambda p: p[0])
        self.times = [p[0] for p in pairs]
        self.values = [p[1] for p in pairs]

    def __len__(self):
        return len(self.times)

    def nearest(self, t, tol_s=ENRICH_TOL_S):
        """Value whose timestamp is closest to t, or None if beyond tolerance."""
        if not self.times:
            return None
        i = bisect.bisect_left(self.times, t)
        best = None
        best_gap = None
        for j in (i - 1, i):
            if 0 <= j < len(self.times):
                gap = abs((self.times[j] - t).total_seconds())
                if best_gap is None or gap < best_gap:
                    best_gap, best = gap, self.values[j]
        if best_gap is not None and best_gap <= tol_s:
            return best
        return None


def read_csv(path):
    """Yield header (list) then dict rows for a semicolon-delimited CSV."""
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f, delimiter=";")
        try:
            header = next(reader)
        except StopIteration:
            return
        yield header
        for row in reader:
            if not row:
                continue
            # Pad/truncate defensively so zip stays aligned.
            if len(row) < len(header):
                row = row + [""] * (len(header) - len(row))
            yield dict(zip(header, row))


def to_float(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Load the workout
# --------------------------------------------------------------------------- #
def load_workout(workout_id):
    rows = read_csv(WORKOUT_CSV)
    try:
        next(rows)  # header
    except StopIteration:
        sys.exit("workout.csv is empty or missing.")
    all_ids = []
    match = None
    for r in rows:
        all_ids.append(r["id"])
        if r["id"] == workout_id:
            match = r
    if match is None:
        sample = "\n  ".join(all_ids[:5])
        sys.exit(
            f"Workout id {workout_id!r} not found in workout.csv.\n"
            f"({len(all_ids)} workouts exist.) Example ids:\n  {sample}"
        )
    start = parse_ts(match["start_datetime"])
    end = parse_ts(match["end_datetime"])
    if start is None or end is None:
        sys.exit(f"Workout {workout_id} has no usable start/end datetime.")
    if end < start:
        start, end = end, start
    return match, start, end


# --------------------------------------------------------------------------- #
# GPX construction with sensor enrichment
# --------------------------------------------------------------------------- #
def load_trackpoints(start, end):
    """Read rawlocation rows in the window as ordered point dicts."""
    path = os.path.join(DATA_DIR, "rawlocation.csv")
    pts = []
    if not os.path.exists(path):
        return pts
    rows = read_csv(path)
    next(rows, None)
    for r in rows:
        t = parse_ts(r.get("timestamp"))
        if not in_window(t, start, end):
            continue
        lat = to_float(r.get("latitude"))
        lon = to_float(r.get("longitude"))
        if lat is None or lon is None:
            continue
        pts.append(
            {
                "t": t,
                "lat": lat,
                "lon": lon,
                "ele": to_float(r.get("altitude")),
                "speed": to_float(r.get("speed")),
            }
        )
    pts.sort(key=lambda p: p["t"])
    return pts


def load_series(filename, value_col, start, end, transform=None):
    """Build a Series of (ts, value) for a lone-timestamp stream in the window."""
    path = os.path.join(DATA_DIR, filename)
    pairs = []
    if not os.path.exists(path):
        return Series(pairs)
    rows = read_csv(path)
    header = next(rows, None)
    if not header or value_col not in header:
        return Series(pairs)
    for r in rows:
        t = parse_ts(r.get("timestamp"))
        if not in_window(t, start, end):
            continue
        val = to_float(r.get(value_col))
        if val is None:
            continue
        pairs.append((t, transform(val, r) if transform else val))
    return Series(pairs)


def load_cadence_series(start, end):
    """Cadence (steps/min) from stepcount buckets, keyed at the bucket midpoint.

    stepcount rows are (timestamp, end_time, steps) buckets. Oura records no true
    cadence, so we derive steps-per-minute over each bucket.
    """
    path = os.path.join(DATA_DIR, "stepcount.csv")
    pairs = []
    if not os.path.exists(path):
        return Series(pairs)
    rows = read_csv(path)
    header = next(rows, None)
    if not header:
        return Series(pairs)
    for r in rows:
        t0 = parse_ts(r.get("timestamp"))
        # stepcount's end_time is Unix epoch milliseconds, not ISO.
        t1 = parse_epoch_ms(r.get("end_time")) or parse_ts(r.get("end_time")) or t0
        if t0 is None:
            continue
        # Keep buckets that overlap the window.
        if not overlaps(t0, t1, start, end):
            continue
        steps = to_float(r.get("steps"))
        if steps is None:
            continue
        secs = max((t1 - t0).total_seconds(), 1.0)
        cad = steps / secs * 60.0
        mid = t0 + (t1 - t0) / 2
        pairs.append((mid, round(cad)))
    # Tolerance is bucket-scale, so allow a wider match for cadence.
    s = Series(pairs)
    return s


def stream_mean(filename, value_col, start, end):
    """Return (mean, count) of a lone-timestamp stream's value in the window."""
    s = load_series(filename, value_col, start, end)
    if len(s) == 0:
        return None, 0
    return sum(s.values) / len(s.values), len(s.values)


def session_summary(workout, start, end):
    """Compute the headline metrics the Oura app shows for a workout.

    Average speed is distance / duration (as the app reports it), not the mean
    of instantaneous GPS speed samples. Average HR is the mean of heart-rate
    samples inside the window (None when the ring logged no HR then).
    """
    dur_s = (end - start).total_seconds()
    cal = to_float(workout.get("calories"))
    dist = to_float(workout.get("distance"))  # meters
    hr_mean, hr_n = stream_mean("heartrate.csv", "bpm", start, end)

    hh = int(dur_s // 3600)
    mm = int((dur_s % 3600) // 60)
    ss = int(round(dur_s % 60))
    duration_hms = f"{hh:d}:{mm:02d}:{ss:02d}"

    meters_per_mile = 1609.344
    avg_speed_ms = dist / dur_s if dist and dur_s > 0 else None
    avg_pace = None
    if avg_speed_ms and avg_speed_ms > 0:
        spm = meters_per_mile / avg_speed_ms  # seconds per mile
        pm, ps = int(spm // 60), int(round(spm % 60))
        if ps == 60:
            pm, ps = pm + 1, 0
        avg_pace = f"{pm}:{ps:02d}"

    return {
        "duration_min": round(dur_s / 60.0, 1),
        "duration_hms": duration_hms,
        "total_calories": round(cal) if cal is not None else None,
        "distance_m": round(dist, 1) if dist else None,
        "distance_mi": round(dist / meters_per_mile, 2) if dist else None,
        "avg_speed_m_s": round(avg_speed_ms, 3) if avg_speed_ms else None,
        "avg_speed_mph": round(avg_speed_ms / meters_per_mile * 3600.0, 2)
        if avg_speed_ms else None,
        "avg_pace_min_per_mile": avg_pace,
        "avg_heart_rate_bpm": round(hr_mean) if hr_mean is not None else None,
        "avg_heart_rate_sample_count": hr_n,
    }


def build_gpx(workout, start, end, points, hr, cad, temp):
    """Return a GPX 1.1 document string with TrackPointExtension enrichment."""
    activity = workout.get("activity") or "workout"
    name = f"{activity} {workout.get('day', '')}".strip()

    def esc(x):
        return sax.escape(str(x))

    def iso(t):
        return t.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    out = []
    out.append('<?xml version="1.0" encoding="UTF-8"?>')
    out.append(
        '<gpx version="1.1" creator="extract_session.py" '
        'xmlns="http://www.topografix.com/GPX/1/1" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1" '
        'xsi:schemaLocation="http://www.topografix.com/GPX/1/1 '
        "http://www.topografix.com/GPX/1/1/gpx.xsd "
        "http://www.garmin.com/xmlschemas/TrackPointExtension/v1 "
        'http://www.garmin.com/xmlschemas/TrackPointExtensionv1.xsd">'
    )
    out.append("  <metadata>")
    out.append(f"    <name>{esc(name)}</name>")
    out.append(
        f"    <desc>Oura workout {esc(workout.get('id',''))} "
        f"({esc(activity)})</desc>"
    )
    out.append(f"    <time>{iso(start)}</time>")
    out.append("  </metadata>")
    out.append("  <trk>")
    out.append(f"    <name>{esc(name)}</name>")
    out.append("    <trkseg>")
    for p in points:
        out.append(f'      <trkpt lat="{p["lat"]:.7f}" lon="{p["lon"]:.7f}">')
        if p["ele"] is not None:
            out.append(f"        <ele>{p['ele']:.1f}</ele>")
        out.append(f"        <time>{iso(p['t'])}</time>")

        hr_v = hr.nearest(p["t"])
        # Cadence buckets are coarse; match within a minute.
        cad_v = cad.nearest(p["t"], tol_s=90.0)
        temp_v = temp.nearest(p["t"], tol_s=300.0)
        spd_v = p["speed"]
        if any(v is not None for v in (hr_v, cad_v, temp_v, spd_v)):
            out.append("        <extensions>")
            out.append("          <gpxtpx:TrackPointExtension>")
            if hr_v is not None:
                out.append(f"            <gpxtpx:hr>{int(round(hr_v))}</gpxtpx:hr>")
            if cad_v is not None:
                out.append(f"            <gpxtpx:cad>{int(cad_v)}</gpxtpx:cad>")
            if temp_v is not None:
                out.append(
                    f"            <gpxtpx:atemp>{temp_v:.1f}</gpxtpx:atemp>"
                )
            if spd_v is not None:
                out.append(f"            <gpxtpx:speed>{spd_v:.2f}</gpxtpx:speed>")
            out.append("          </gpxtpx:TrackPointExtension>")
            out.append("        </extensions>")
        out.append("      </trkpt>")
    out.append("    </trkseg>")
    out.append("  </trk>")
    out.append("</gpx>")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
# Generic per-file scan for the sidecar dump
# --------------------------------------------------------------------------- #
ID_COLS = ("id", "workout_identifier", "selected_activity_id")
START_END_PAIRS = [
    ("start_datetime", "end_datetime"),
    ("start_time", "end_time"),
    ("bedtime_start", "bedtime_end"),
]
JSON_COLS = ("contributors",)  # embedded JSON we can pretty-parse


def numeric_summary(rows, header):
    """min/max/mean for numeric columns across matched rows."""
    stats = {}
    for col in header:
        vals = [to_float(r.get(col)) for r in rows]
        vals = [v for v in vals if v is not None]
        if len(vals) >= 2 and len(vals) == len(
            [r for r in rows if r.get(col) not in (None, "")]
        ):
            stats[col] = {
                "min": min(vals),
                "max": max(vals),
                "mean": sum(vals) / len(vals),
                "count": len(vals),
            }
    return stats


def parse_json_cols(row):
    """Return a shallow copy with embedded-JSON columns parsed."""
    out = dict(row)
    for col in JSON_COLS:
        if col in out and out[col]:
            try:
                out[col] = json.loads(out[col])
            except (ValueError, TypeError):
                pass
    return out


def time_bounds(rows, header):
    """First/last timestamp string across a matched set, if any time col."""
    tcol = "timestamp" if "timestamp" in header else None
    if tcol is None:
        for a, _b in START_END_PAIRS:
            if a in header:
                tcol = a
                break
    if tcol is None:
        return None
    stamps = sorted(r.get(tcol, "") for r in rows if r.get(tcol))
    if not stamps:
        return None
    return {"column": tcol, "first": stamps[0], "last": stamps[-1]}


def scan_file(path, workout, start, end):
    """Classify one CSV and return a match record for the sidecars."""
    name = os.path.basename(path)
    rows_iter = read_csv(path)
    header = next(rows_iter, None)
    if header is None:
        return {"file": name, "strategy": "empty", "match_count": 0, "note": "no header"}

    id_col = next((c for c in ID_COLS if c in header), None)
    has_ts = "timestamp" in header
    pair = next((p for p in START_END_PAIRS if p[0] in header), None)
    has_day = "day" in header
    wid = workout["id"]
    wday = workout.get("day")

    # Decide the time-join strategy independently of the (own) id column, so
    # daily files that carry their own id still fall through to day-match.
    # A file with BOTH `day` and `timestamp` is a daily summary (one marker
    # timestamp per day) -> match on `day`, not the intraday window.
    if has_day and has_ts:
        mode = "day"
        time_strategy = "day-match"
    elif has_ts:
        mode = "ts"
        time_strategy = "point-in-window"
    elif pair:
        mode = "pair"
        time_strategy = f"interval-overlap ({pair[0]}/{pair[1]})"
    elif has_day:
        mode = "day"
        time_strategy = "day-match"
    else:
        mode = "none"
        time_strategy = "no-join-key"

    # Single pass: gather id-equals-workout hits and time-based hits at once.
    id_hits, time_hits = [], []
    for r in rows_iter:
        if id_col and r.get(id_col) == wid:
            id_hits.append(r)
        if mode == "ts":
            if in_window(parse_ts(r.get("timestamp")), start, end):
                time_hits.append(r)
        elif mode == "pair":
            a, b = pair
            if overlaps(parse_ts(r.get(a)), parse_ts(r.get(b)), start, end):
                time_hits.append(r)
        elif mode == "day" and r.get("day") == wday:
            time_hits.append(r)

    # A real id match (today only workout.csv matching itself, or a future
    # foreign key) is the most precise link; otherwise use the time join.
    if id_hits:
        strategy = f"id-match ({id_col})"
        matched = id_hits
    else:
        strategy = time_strategy
        matched = time_hits

    rec = {
        "file": name,
        "strategy": strategy,
        "columns": header,
        "match_count": len(matched),
    }
    if matched:
        rec["numeric_summary"] = numeric_summary(matched, header)
        tb = time_bounds(matched, header)
        if tb:
            rec["time_bounds"] = tb
        if len(matched) <= ROW_CAP:
            rec["rows"] = [parse_json_cols(r) for r in matched]
        else:
            rec["rows_sample"] = {
                "first": parse_json_cols(matched[0]),
                "last": parse_json_cols(matched[-1]),
                "note": f"{len(matched)} rows; showing first/last only",
            }
    return rec


# --------------------------------------------------------------------------- #
# Markdown rendering
# --------------------------------------------------------------------------- #
def render_markdown(workout, start, end, gps_count, scans, summary):
    L = []
    wid = workout["id"]
    L.append(f"# Oura session {wid}")
    L.append("")
    L.append(f"- **Activity:** {workout.get('activity','')}")
    L.append(f"- **Day:** {workout.get('day','')}")
    L.append(f"- **Start (UTC):** {start.isoformat()}")
    L.append(f"- **End (UTC):** {end.isoformat()}")
    for k in ("intensity", "label", "source"):
        v = workout.get(k)
        if v:
            L.append(f"- **{k.capitalize()}:** {v}")
    L.append(f"- **GPS trackpoints:** {gps_count}")
    L.append("")

    # Headline metrics, as the Oura app presents them.
    def fmt(v, unit=""):
        return "—" if v is None else f"{v}{unit}"

    L.append("## Session summary")
    L.append("")
    L.append("| Metric | Value |")
    L.append("| --- | --- |")
    L.append(f"| Duration | {summary['duration_hms']} "
             f"({fmt(summary['duration_min'],' min')}) |")
    L.append(f"| Total calories | {fmt(summary['total_calories'])} |")
    L.append(f"| Distance | {fmt(summary['distance_mi'],' mi')} |")
    L.append(f"| Average speed | {fmt(summary['avg_speed_mph'],' mph')} |")
    L.append(f"| Average pace | {fmt(summary['avg_pace_min_per_mile'],' /mi')} |")
    hr_txt = fmt(summary["avg_heart_rate_bpm"], " bpm")
    if summary["avg_heart_rate_bpm"] is not None:
        hr_txt += f" (n={summary['avg_heart_rate_sample_count']})"
    L.append(f"| Average heart rate | {hr_txt} |")
    L.append("")
    L.append("> Average speed/pace is distance ÷ duration (as the Oura app "
             "reports it), not the mean of instantaneous GPS speed samples.")
    L.append("")
    L.append("> Cadence is derived steps/min (Oura has no true cadence); "
             "`atemp` is **skin** temperature, not ambient.")
    L.append("")

    hits = [s for s in scans if s.get("match_count", 0) > 0]
    misses = [s for s in scans if s.get("match_count", 0) == 0]

    L.append("## Files with data")
    L.append("")
    L.append("| File | Strategy | Rows |")
    L.append("| --- | --- | --- |")
    for s in sorted(hits, key=lambda x: -x["match_count"]):
        L.append(f"| `{s['file']}` | {s['strategy']} | {s['match_count']} |")
    L.append("")

    for s in sorted(hits, key=lambda x: -x["match_count"]):
        L.append(f"### `{s['file']}` — {s['match_count']} row(s)")
        L.append(f"*Strategy: {s['strategy']}*")
        tb = s.get("time_bounds")
        if tb:
            L.append(f"Time span ({tb['column']}): {tb['first']} → {tb['last']}")
        ns = s.get("numeric_summary") or {}
        if ns:
            L.append("")
            L.append("| Column | min | max | mean |")
            L.append("| --- | --- | --- | --- |")
            for col, st in ns.items():
                L.append(
                    f"| {col} | {st['min']:.3g} | {st['max']:.3g} | "
                    f"{st['mean']:.3g} |"
                )
        if "rows" in s and len(s["rows"]) <= 10:
            L.append("")
            L.append("<details><summary>rows</summary>")
            L.append("")
            L.append("```json")
            L.append(json.dumps(s["rows"], indent=2, default=str))
            L.append("```")
            L.append("</details>")
        L.append("")

    L.append("## Files with no matching data")
    L.append("")
    L.append(", ".join(f"`{s['file']}`" for s in misses) or "_none_")
    L.append("")
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("workout_id", help="UUID from workout.csv")
    ap.add_argument(
        "--outdir",
        default=os.path.join(REPO_ROOT, "sessions"),
        help="output directory (default: <repo>/sessions)",
    )
    args = ap.parse_args(argv)

    if not os.path.isdir(DATA_DIR):
        sys.exit(f"Data directory not found: {DATA_DIR}")

    workout, start, end = load_workout(args.workout_id)
    outdir = os.path.join(args.outdir, args.workout_id)
    os.makedirs(outdir, exist_ok=True)

    # --- Build GPX (or warn/skip) ---
    points = load_trackpoints(start, end)
    gpx_path = os.path.join(outdir, f"{args.workout_id}.gpx")
    if points:
        hr = load_series("heartrate.csv", "bpm", start, end)
        temp = load_series("temperature.csv", "skin_temp", start, end)
        # Only derive cadence for gait activities; steps are noise otherwise.
        activity = (workout.get("activity") or "").lower()
        cadence_ok = activity in CADENCE_ACTIVITIES
        cad = load_cadence_series(start, end) if cadence_ok else Series([])
        gpx = build_gpx(workout, start, end, points, hr, cad, temp)
        with open(gpx_path, "w", encoding="utf-8") as f:
            f.write(gpx)
        cad_note = len(cad) if cadence_ok else f"omitted ({activity})"
        print(
            f"GPX: {len(points)} trackpoints "
            f"(HR:{len(hr)} temp:{len(temp)} cad:{cad_note}) -> {gpx_path}"
        )
    else:
        gpx_path = None
        print(
            "WARNING: no GPS points in this workout's window "
            "(indoor, or before the location stream begins ~2025-04-16). "
            "Skipping GPX; sidecars still written."
        )

    # --- Scan every CSV for the sidecars ---
    scans = []
    for path in sorted(glob.glob(os.path.join(DATA_DIR, "*.csv"))):
        scans.append(scan_file(path, workout, start, end))

    summary = session_summary(workout, start, end)

    payload = {
        "workout": workout,
        "window_utc": {"start": start.isoformat(), "end": end.isoformat()},
        "session_summary": summary,
        "gps_trackpoints": len(points),
        "gpx_file": os.path.basename(gpx_path) if gpx_path else None,
        "files": scans,
    }
    json_path = os.path.join(outdir, f"{args.workout_id}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)
    print(f"JSON: {json_path}")

    md_path = os.path.join(outdir, f"{args.workout_id}.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(render_markdown(workout, start, end, len(points), scans, summary))
    print(f"MD:   {md_path}")

    hits = sum(1 for s in scans if s.get("match_count", 0) > 0)
    print(f"Scanned {len(scans)} files; {hits} had matching data.")


if __name__ == "__main__":
    main()
