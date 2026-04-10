import csv
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
    for fmt in ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y"):
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
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
def _parse_csv(file_content):
    """Parse Redis CSV: extract date + status_account columns."""
    reader = csv.DictReader(io.StringIO(file_content))
    rows = []
    for row in reader:
        date_val = row.get("date", "")
        status_val = row.get("status_account", "").strip()
        dt = _parse_date(date_val)
        if dt is not None and status_val:
            rows.append((dt, status_val))
    return rows


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


def _build_response(rows, monday, sunday, session_id, weeks):
    """Build the JSON response for Redis workflow."""
    counts = _count_statuses(rows, monday, sunday)
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
def _parse_harvey_csv(file_content):
    """Parse Harvey CSV: extract date_day, legal_team_type, organization_type, normalized_country.

    Returns list of (datetime, legal_team_type, org_type, country) tuples.
    """
    reader = csv.DictReader(io.StringIO(file_content))
    rows = []
    for row in reader:
        date_val = row.get("date_day", "")
        dt = _parse_date(date_val)
        if dt is None:
            continue
        legal_team_type = row.get("legal_team_type", "").strip()
        org_type = row.get("organization_type", "").strip()
        country = row.get("normalized_country", "").strip()
        rows.append((dt, legal_team_type, org_type, country))
    return rows


def _count_harvey(rows, monday, sunday):
    """Filter Harvey rows to Mon-Sun range and compute breakdowns.

    Returns dict with total_file, filtered, account_types, law_firm_subtypes, regions.
    Each breakdown has items with label, count, pct.
    """
    total_file = len(rows)
    start = monday.replace(hour=0, minute=0, second=0)
    end = sunday.replace(hour=23, minute=59, second=59)

    filtered = [r for r in rows if start <= r[0] <= end]
    total = len(filtered)

    # --- Account types ---
    acct_counts = {"In-House": 0, "Law Firm": 0, "Asset Management": 0, "Unclassified": 0}
    for _, legal_team_type, _, _ in filtered:
        if legal_team_type == "In-House":
            acct_counts["In-House"] += 1
        elif legal_team_type == "Law Firm":
            acct_counts["Law Firm"] += 1
        elif legal_team_type == "Asset Management":
            acct_counts["Asset Management"] += 1
        else:
            acct_counts["Unclassified"] += 1

    account_types = []
    for label in ["In-House", "Law Firm", "Asset Management", "Unclassified"]:
        count = acct_counts[label]
        pct = round(count * 100 / total) if total else 0
        account_types.append({"label": label, "count": count, "pct": pct})

    # --- Law Firm sub-types (only rows where legal_team_type == "Law Firm") ---
    lf_rows = [r for r in filtered if r[1] == "Law Firm"]
    lf_total = len(lf_rows)
    lf_counts = {"Full Service": 0, "Litigation": 0, "Transactional": 0}
    for _, _, org_type, _ in lf_rows:
        if org_type == "Full Service Law Firm":
            lf_counts["Full Service"] += 1
        elif org_type == "Litigation Law Firm":
            lf_counts["Litigation"] += 1
        elif org_type == "Transactional Law Firm":
            lf_counts["Transactional"] += 1

    law_firm_subtypes = []
    for label in ["Full Service", "Litigation", "Transactional"]:
        count = lf_counts[label]
        pct = round(count * 100 / lf_total) if lf_total else 0
        law_firm_subtypes.append({"label": label, "count": count, "pct": pct})

    # --- Regions ---
    region_counts = {"AMER": 0, "EMEA": 0, "ROW": 0}
    for _, _, _, country in filtered:
        region_counts[_country_to_region(country)] += 1

    regions = []
    for label in ["AMER", "EMEA", "ROW"]:
        count = region_counts[label]
        pct = round(count * 100 / total) if total else 0
        regions.append({"label": label, "count": count, "pct": pct})

    return {
        "total_file": total_file,
        "filtered": total,
        "account_types": account_types,
        "law_firm_subtypes": law_firm_subtypes,
        "regions": regions,
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

    try:
        content = file.read().decode("utf-8", errors="replace")
    except Exception:
        return jsonify({"error": "Could not read file. Make sure it's a valid CSV."}), 400

    # Parse with the right parser
    if workflow == "harvey":
        rows = _parse_harvey_csv(content)
        error_msg = "No valid rows found. Check that the CSV has a 'date_day' column."
    else:
        rows = _parse_csv(content)
        error_msg = "No valid rows found. Check that the CSV has 'date' and 'status_account' columns."

    if not rows:
        return jsonify({"error": error_msg}), 400

    weeks = _get_week_ranges(rows)
    if not weeks:
        return jsonify({"error": "Could not determine any week ranges from the data."}), 400

    best_week = _detect_best_week(weeks)
    monday, sunday = best_week

    session_id = str(uuid.uuid4())
    _sessions[session_id] = {"data": rows, "workflow": workflow, "created": time.time()}

    if workflow == "harvey":
        return jsonify(_build_harvey_response(rows, monday, sunday, session_id, weeks))
    return jsonify(_build_response(rows, monday, sunday, session_id, weeks))


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
    weeks = _get_week_ranges(rows)

    if workflow == "harvey":
        return jsonify(_build_harvey_response(rows, monday, sunday, session_id, weeks))
    return jsonify(_build_response(rows, monday, sunday, session_id, weeks))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_ENV") != "production"
    app.run(host="0.0.0.0", port=port, debug=debug)
