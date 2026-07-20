import csv
import gc
import io
import os
import sys
import time
import uuid
from datetime import datetime, timedelta

from flask import Flask, jsonify, render_template, request

# Harvey CSV has very large reasoning columns
csv.field_size_limit(10_000_000)

app = Flask(__name__)

# Reject uploads larger than 60 MB before reading into memory
MAX_UPLOAD_BYTES = 60 * 1024 * 1024
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

# In-memory store for parsed CSV data, keyed by session ID.
# Each entry: {"data": [...], "workflow": "redis"|"harvey", "created": timestamp}
_sessions = {}
_SESSION_TTL = 3600  # 1 hour

# ---------------------------------------------------------------------------
# Country-to-region mapping for Harvey workflow
# ---------------------------------------------------------------------------
_AMER_COUNTRIES = {
    "United States", "Canada", "Brazil", "Mexico", "Argentina", "Colombia",
    "Chile", "Peru", "Ecuador", "Panama", "Dominican Republic", "Puerto Rico",
    "Uruguay", "Costa Rica", "Guatemala", "El Salvador", "Venezuela",
    "Honduras", "Paraguay", "Jamaica", "Guyana", "Nicaragua",
    "Trinidad and Tobago", "Cayman Islands", "Bermuda",
    "Turks and Caicos Islands", "Virgin Islands British", "Martinique",
}

_EMEA_COUNTRIES = {
    "United Kingdom", "France", "Spain", "Germany", "Italy", "Portugal",
    "Denmark", "Netherlands", "United Arab Emirates", "South Africa",
    "Switzerland", "Sweden", "Israel", "Belgium", "Turkey", "Finland",
    "Saudi Arabia", "Ireland", "Poland", "Austria", "Greece", "Romania",
    "Estonia", "Cyprus", "Egypt", "Luxembourg", "Norway", "Ukraine",
    "Czechia", "Nigeria", "Kenya", "Uganda", "Gibraltar", "Bulgaria",
    "Malta", "Lithuania", "Morocco", "Hungary", "Qatar", "Slovakia",
    "Slovenia", "Serbia", "Croatia", "Oman", "Algeria", "Latvia",
    "Liechtenstein", "Jordan", "Kuwait", "Iraq", "Bahrain", "Cameroon",
    "Angola", "Monaco", "Albania", "Jersey", "Guernsey", "Montenegro",
    "Bosnia and Herzegovina", "Sierra Leone", "Senegal", "Ghana", "Tunisia",
    "Ethiopia", "Cote d'Ivoire", "Azerbaijan", "Lebanon", "Togo",
    "Mauritius", "Rwanda", "Congo", "Congo the Democratic Republic of the",
    "Russia", "Madagascar", "Zambia", "Kazakhstan", "Gabon", "Iran",
    "Afghanistan",
}


def _country_to_region(country):
    """Map a country name to AMER, EMEA, or ROW."""
    if country in _AMER_COUNTRIES:
        return "AMER"
    if country in _EMEA_COUNTRIES:
        return "EMEA"
    return "ROW"


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------
def _cleanup_sessions():
    """Remove sessions older than TTL."""
    now = time.time()
    expired = [k for k, v in _sessions.items() if now - v["created"] > _SESSION_TTL]
    for k in expired:
        del _sessions[k]


# ---------------------------------------------------------------------------
# Shared date helpers
# ---------------------------------------------------------------------------
def _parse_date(date_str):
    """Parse date string trying multiple formats. Returns datetime or None."""
    date_str = date_str.strip()
    if not date_str:
        return None
    for fmt in (
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M",
        "%m/%d/%Y",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%m/%d/%Y %I:%M:%S %p",
        "%m/%d/%Y %I:%M %p",
    ):
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    # Handle milliseconds: strip .000 suffix and retry
    if "." in date_str:
        truncated = date_str.rsplit(".", 1)[0]
        return _parse_date(truncated)
    return None


def _get_monday(dt):
    """Return the Monday (start of week) for a given date."""
    return (dt - timedelta(days=dt.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def _get_week_ranges(rows):
    """Compute all Monday-Sunday week ranges from the data.

    Returns a list of (monday_date, sunday_date) sorted chronologically.
    """
    if not rows:
        return []
    dates = [r[0] for r in rows]
    min_date = min(dates)
    max_date = max(dates)

    current_monday = _get_monday(min_date)
    max_monday = _get_monday(max_date)

    weeks = []
    while current_monday <= max_monday:
        sunday = current_monday + timedelta(days=6)
        weeks.append((current_monday, sunday))
        current_monday += timedelta(days=7)
    return weeks


def _detect_best_week(weeks):
    """Pick the last complete Mon-Sun range (Sunday <= today)."""
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    complete = [w for w in weeks if w[1] <= today]
    if complete:
        return complete[-1]
    return weeks[-1] if weeks else None


def _format_week_label(monday, sunday):
    """Format a week range as 'Mon Mar 30 to Sun Apr 5'."""
    return f"{monday.strftime('%a %b %-d')} to {sunday.strftime('%a %b %-d')}"


# ---------------------------------------------------------------------------
# Redis workflow (existing)
# ---------------------------------------------------------------------------
def _parse_csv(file_stream):
    """Parse Redis CSV row-by-row, keeping only date + status_account.

    Uses csv.reader with index lookup instead of DictReader to avoid
    loading all columns into memory (important for large files).
    """
    text_stream = io.TextIOWrapper(file_stream, encoding="utf-8-sig", errors="replace")
    reader = csv.reader(text_stream)

    # Read header and find column indices
    try:
        header = next(reader)
    except StopIteration:
        return [], {"skipped_bad_date": 0, "skipped_empty_status": 0, "skipped_short_row": 0}
    col_map = {name.strip(): i for i, name in enumerate(header)}
    date_idx = col_map.get("date")
    status_idx = col_map.get("status_account")
    if date_idx is None or status_idx is None:
        return [], {"skipped_bad_date": 0, "skipped_empty_status": 0, "skipped_short_row": 0}

    rows = []
    skipped_bad_date = 0
    skipped_empty_status = 0
    skipped_short_row = 0
    for fields in reader:
        if len(fields) <= max(date_idx, status_idx):
            skipped_short_row += 1
            continue
        dt = _parse_date(fields[date_idx])
        status_val = fields[status_idx].strip()
        if dt is None:
            skipped_bad_date += 1
            continue
        if not status_val:
            skipped_empty_status += 1
            continue
        rows.append((dt, status_val))
    skip_info = {
        "skipped_bad_date": skipped_bad_date,
        "skipped_empty_status": skipped_empty_status,
        "skipped_short_row": skipped_short_row,
    }
    return rows, skip_info


def _count_statuses(rows, monday, sunday):
    """Filter rows to a Mon-Sun range and count each status_account value."""
    total_file = len(rows)
    start = monday.replace(hour=0, minute=0, second=0)
    end = sunday.replace(hour=23, minute=59, second=59)

    filtered_rows = [(dt, s) for dt, s in rows if start <= dt <= end]

    raw_counts = {}
    for _, status in filtered_rows:
        label = status.strip()
        raw_counts[label] = raw_counts.get(label, 0) + 1

    status_counts = [
        {"label": label, "count": count}
        for label, count in sorted(raw_counts.items(), key=lambda x: -x[1])
    ]

    lookup = {k.lower(): v for k, v in raw_counts.items()}
    enriched_auto = lookup.get("enriched: auto", 0)
    needs_enrichment = lookup.get("needs enrichment", 0)
    junk = lookup.get("junk", 0)
    duplicate = lookup.get("duplicate", 0)

    return {
        "total_file": total_file,
        "filtered": len(filtered_rows),
        "status_counts": status_counts,
        "enriched_validated": enriched_auto + needs_enrichment,
        "junk": junk,
        "duplicate": duplicate,
    }


def _build_response(rows, monday, sunday, session_id, weeks, skip_info=None):
    """Build the JSON response for Redis workflow."""
    counts = _count_statuses(rows, monday, sunday)
    if skip_info:
        counts["skipped_rows"] = skip_info
    week_options = [
        {
            "label": _format_week_label(m, s),
            "monday": m.strftime("%Y-%m-%d"),
            "sunday": s.strftime("%Y-%m-%d"),
        }
        for m, s in weeks
    ]
    return {
        "session_id": session_id,
        "workflow": "redis",
        "selected_week": {
            "label": _format_week_label(monday, sunday),
            "monday": monday.strftime("%Y-%m-%d"),
            "sunday": sunday.strftime("%Y-%m-%d"),
        },
        "weeks": week_options,
        "counts": counts,
    }


# ---------------------------------------------------------------------------
# Harvey workflow (new)
# ---------------------------------------------------------------------------
def _parse_harvey_csv(file_stream):
    """Parse Harvey CSV row-by-row, keeping only the columns we need.

    Uses csv.reader with index lookup so the large reasoning columns
    are discarded immediately and never stored in memory. Each returned
    row is (date, account_type, country, aum, headcount_confidence).
    """
    # Decode the upload line-by-line straight from the byte stream. This keeps
    # the row-by-row streaming (never loads the whole file) and, unlike
    # io.TextIOWrapper, works regardless of stream type / Python version
    # (Flask spools large uploads to a SpooledTemporaryFile, which lacks
    # .readable() on Python < 3.11). Stray NUL bytes are stripped so csv
    # doesn't raise "line contains NUL"; csv.reader reassembles quoted
    # multi-line fields from this iterator.
    def _lines(byte_stream):
        first = True
        for raw in byte_stream:
            text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
            if first:
                first = False
                if text[:1] == "﻿":  # strip UTF-8 BOM
                    text = text[1:]
            yield text.replace("\x00", "")

    reader = csv.reader(_lines(file_stream))

    try:
        header = next(reader)
    except StopIteration:
        return []
    col_map = {name.strip(): i for i, name in enumerate(header)}
    date_idx = col_map.get("date_day")
    if date_idx is None:
        return []
    acct_idx = col_map.get("reconciled_legal_team_type")
    co_idx = col_map.get("normalized_country")
    aum_idx = col_map.get("AUM")
    conf_idx = col_map.get("Agentic_Lawyer_Headcount_Confidence")

    def get(fields, idx):
        return fields[idx].strip() if idx is not None and idx < len(fields) else ""

    rows = []
    for fields in reader:
        if len(fields) <= date_idx:
            continue
        dt = _parse_date(fields[date_idx])
        if dt is None:
            continue
        account_type = get(fields, acct_idx)
        country = get(fields, co_idx)
        aum = get(fields, aum_idx)
        confidence = get(fields, conf_idx)
        rows.append((dt, account_type, country, aum, confidence))
    return rows


def _count_harvey(rows, monday, sunday):
    """Filter Harvey rows to Mon-Sun range and compute breakdowns.

    Returns dict with total_file, filtered, account_types, regions,
    headcount_confidence. Rows are (date, account_type, country, aum, confidence).
    """
    total_file = len(rows)
    start = monday.replace(hour=0, minute=0, second=0)
    end = sunday.replace(hour=23, minute=59, second=59)

    filtered = [r for r in rows if start <= r[0] <= end]
    total = len(filtered)

    def pct_of(count, denom):
        return round(count * 100 / denom) if denom else 0

    # --- Account types: show ALL values found (blank -> "Unclassified") ---
    acct_counts = {}
    for _, account_type, _, _, _ in filtered:
        label = account_type if account_type else "Unclassified"
        acct_counts[label] = acct_counts.get(label, 0) + 1

    # AUM enrichment among Asset Management rows only.
    am_rows = [r for r in filtered if r[1] == "Asset Management"]
    am_total = len(am_rows)
    am_with_aum = sum(1 for r in am_rows if r[3])

    # Order: all real types by count desc, then "Unclassified" last.
    ordered = sorted(
        (l for l in acct_counts if l != "Unclassified"),
        key=lambda l: -acct_counts[l],
    )
    if "Unclassified" in acct_counts:
        ordered.append("Unclassified")

    account_types = []
    for label in ordered:
        count = acct_counts[label]
        entry = {"label": label, "count": count, "pct": pct_of(count, total)}
        if label == "Asset Management":
            entry["aum_enriched"] = {
                "count": am_with_aum,
                "pct": pct_of(am_with_aum, am_total),
            }
        account_types.append(entry)

    # --- Regions ---
    region_counts = {"AMER": 0, "EMEA": 0, "ROW": 0}
    for _, _, country, _, _ in filtered:
        region_counts[_country_to_region(country)] += 1

    regions = [
        {"label": label, "count": region_counts[label], "pct": pct_of(region_counts[label], total)}
        for label in ["AMER", "EMEA", "ROW"]
    ]

    # --- Lawyer headcount enrichment ---
    # Count non-empty confidence values, then show only meaningful categories:
    # drop error/timeout states and anything that rounds to 0%.
    conf_counts = {}
    for _, _, _, _, confidence in filtered:
        if confidence:
            conf_counts[confidence] = conf_counts.get(confidence, 0) + 1

    error_states = {"TIMEOUT", "UNSTRUCTURED_RESPONSE"}
    headcount_confidence = [
        {"label": label, "count": count, "pct": pct_of(count, total)}
        for label, count in sorted(conf_counts.items(), key=lambda x: -x[1])
        if label not in error_states and pct_of(count, total) > 0
    ]

    return {
        "total_file": total_file,
        "filtered": total,
        "account_types": account_types,
        "regions": regions,
        "headcount_confidence": headcount_confidence,
    }


def _build_harvey_response(rows, monday, sunday, session_id, weeks):
    """Build the JSON response for Harvey workflow."""
    counts = _count_harvey(rows, monday, sunday)
    week_options = [
        {
            "label": _format_week_label(m, s),
            "monday": m.strftime("%Y-%m-%d"),
            "sunday": s.strftime("%Y-%m-%d"),
        }
        for m, s in weeks
    ]
    return {
        "session_id": session_id,
        "workflow": "harvey",
        "selected_week": {
            "label": _format_week_label(monday, sunday),
            "monday": monday.strftime("%Y-%m-%d"),
            "sunday": sunday.strftime("%Y-%m-%d"),
        },
        "weeks": week_options,
        "counts": counts,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.errorhandler(413)
def file_too_large(e):
    return jsonify({"error": "File too large. Maximum size is 60 MB."}), 413


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/upload", methods=["POST"])
def upload():
    _cleanup_sessions()

    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "No file selected"}), 400

    workflow = request.form.get("workflow", "redis")
    if workflow not in ("redis", "harvey"):
        return jsonify({"error": "Unknown workflow"}), 400

    # Stream directly from the upload — never load the full file as a string.
    # csv.reader reads row-by-row; parsers extract only the columns we need.
    skip_info = None
    try:
        if workflow == "harvey":
            rows = _parse_harvey_csv(file.stream)
            error_msg = "No valid rows found. Check that the CSV has a 'date_day' column."
        else:
            rows, skip_info = _parse_csv(file.stream)
            error_msg = "No valid rows found. Check that the CSV has 'date' and 'status_account' columns."
    except Exception:
        return jsonify({"error": "Could not read file. Make sure it's a valid CSV."}), 400
    finally:
        file.close()
        gc.collect()

    if not rows:
        return jsonify({"error": error_msg}), 400

    weeks = _get_week_ranges(rows)
    if not weeks:
        return jsonify({"error": "Could not determine any week ranges from the data."}), 400

    best_week = _detect_best_week(weeks)
    monday, sunday = best_week

    session_id = str(uuid.uuid4())
    _sessions[session_id] = {
        "data": rows, "workflow": workflow, "created": time.time(),
        "skip_info": skip_info,
    }

    if workflow == "harvey":
        return jsonify(_build_harvey_response(rows, monday, sunday, session_id, weeks))
    return jsonify(_build_response(rows, monday, sunday, session_id, weeks, skip_info))


@app.route("/refilter", methods=["POST"])
def refilter():
    _cleanup_sessions()

    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid request"}), 400

    session_id = data.get("session_id")
    monday_str = data.get("monday")
    sunday_str = data.get("sunday")

    if not session_id or not monday_str or not sunday_str:
        return jsonify({"error": "Missing session_id, monday, or sunday"}), 400

    session = _sessions.get(session_id)
    if not session:
        return jsonify({"error": "Session expired. Please re-upload the file."}), 410

    try:
        monday = datetime.strptime(monday_str, "%Y-%m-%d")
        sunday = datetime.strptime(sunday_str, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "Invalid date format"}), 400

    rows = session["data"]
    workflow = session.get("workflow", "redis")
    skip_info = session.get("skip_info")
    weeks = _get_week_ranges(rows)

    if workflow == "harvey":
        return jsonify(_build_harvey_response(rows, monday, sunday, session_id, weeks))
    return jsonify(_build_response(rows, monday, sunday, session_id, weeks, skip_info))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_ENV") != "production"
    app.run(host="0.0.0.0", port=port, debug=debug)
