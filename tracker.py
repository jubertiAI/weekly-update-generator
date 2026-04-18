#!/usr/bin/env python3
"""
Flight price tracker: EZE -> Paris (CDG/ORY/BVA) round trip.
Runs as a Flask web service with APScheduler handling scheduled checks.
Searches via SerpApi Google Flights, filters by time/stops/layover,
and sends Spanish-language price alerts to a Telegram chat.
"""

import os
import sys
import csv
import requests
from datetime import datetime
from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz


# ── Configuration ──────────────────────────────────────────────────
SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = "-5144658730"

# Route & dates
DEPARTURE_ID = "EZE"
ARRIVAL_ID = "CDG,ORY,BVA"
OUTBOUND_DATE = "2026-08-08"
RETURN_DATE = "2026-08-16"

# Budget thresholds (USD)
BUDGET_USD = 1000
URGENT_THRESHOLD = 900   # Below this = urgent alert

# Filters
MIN_OUTBOUND_HOUR = 18        # Outbound must depart at 18:00 or later
MAX_LAYOVER_MINUTES = 240     # 4 hours max layover for 1-stop flights
MAX_STOPS = 1                 # Direct or 1 stop only

# CSV log
CSV_FILE = "paris_price_history.csv"

# Timezone for scheduling
BUE_TZ = pytz.timezone("America/Argentina/Buenos_Aires")


# ── Flask app ────────────────────────────────────────────────────
app = Flask(__name__)


@app.route("/")
def health():
    return "Flight tracker running"


# ── SerpApi search ────────────────────────────────────────────────
def search_flights():
    """Call SerpApi Google Flights for EZE -> Paris round trip."""
    if not SERPAPI_KEY:
        print("ERROR: SERPAPI_KEY environment variable not set")
        return {}

    params = {
        "engine": "google_flights",
        "type": "1",              # Round trip
        "departure_id": DEPARTURE_ID,
        "arrival_id": ARRIVAL_ID,
        "outbound_date": OUTBOUND_DATE,
        "return_date": RETURN_DATE,
        "currency": "USD",
        "hl": "es",
        "gl": "ar",
        "api_key": SERPAPI_KEY,
    }

    resp = requests.get("https://serpapi.com/search", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


# ── Parsing & filtering ──────────────────────────────────────────
def extract_departure_hour(segments):
    """Get the departure hour (int) from the first segment's time string.
    SerpApi format: '2026-08-08 21:30'."""
    time_str = segments[0].get("departure_airport", {}).get("time", "")
    if not time_str:
        return None
    try:
        return int(time_str.strip().split(" ")[-1].split(":")[0])
    except (ValueError, IndexError):
        return None


def parse_flights(data):
    """Parse SerpApi response and return flights matching our filters,
    sorted by price (cheapest first).

    SerpApi returns outbound leg segments in each result's 'flights' array.
    The 'price' is the round-trip total. Layovers between segments are in
    the result's 'layovers' array.
    """
    results = []
    all_flights = data.get("best_flights", []) + data.get("other_flights", [])

    for flight in all_flights:
        price = flight.get("price")
        if price is None:
            continue

        segments = flight.get("flights", [])
        if not segments:
            continue

        num_stops = len(segments) - 1

        # Filter: max 1 stop
        if num_stops > MAX_STOPS:
            continue

        # Filter: outbound departure at 18:00 or later
        dep_hour = extract_departure_hour(segments)
        if dep_hour is None or dep_hour < MIN_OUTBOUND_HOUR:
            continue

        # Filter: layover under 4 hours for 1-stop flights
        layovers = flight.get("layovers", [])
        if num_stops == 1:
            if not layovers:
                continue  # Can't verify layover duration, skip
            layover_duration = layovers[0].get("duration", 0)
            if layover_duration > MAX_LAYOVER_MINUTES:
                continue

        # Extract outbound leg info
        first_seg = segments[0]
        last_seg = segments[-1]

        outbound = {
            "departure_time": first_seg.get("departure_airport", {}).get("time", ""),
            "departure_airport": first_seg.get("departure_airport", {}).get("id", ""),
            "arrival_time": last_seg.get("arrival_airport", {}).get("time", ""),
            "arrival_airport": last_seg.get("arrival_airport", {}).get("id", ""),
            "airline": first_seg.get("airline", ""),
            "num_stops": num_stops,
            "layover_city": "",
            "layover_duration": 0,
        }

        # Add layover details for 1-stop flights
        if num_stops == 1 and layovers:
            outbound["layover_city"] = layovers[0].get("name", "")
            outbound["layover_duration"] = layovers[0].get("duration", 0)

        # Build booking link
        booking_token = flight.get("booking_token", "")
        if booking_token:
            booking_link = (
                f"https://www.google.com/travel/flights/booking?token={booking_token}"
            )
        else:
            booking_link = (
                "https://www.google.com/travel/flights?q="
                f"Flights+EZE+to+Paris+{OUTBOUND_DATE}+return+{RETURN_DATE}"
            )

        results.append({
            "price": price,
            "outbound": outbound,
            "booking_link": booking_link,
        })

    results.sort(key=lambda x: x["price"])
    return results


# ── Formatting helpers ────────────────────────────────────────────
def format_time(time_str):
    """Extract just the time from '2026-08-08 21:30' -> '21:30'."""
    if not time_str:
        return "?"
    parts = time_str.strip().split(" ")
    return parts[-1] if len(parts) >= 2 else time_str


def format_date_short(time_str):
    """Format '2026-08-08 21:30' -> 'vie 8/8'."""
    if not time_str:
        return "?"
    try:
        dt = datetime.strptime(time_str.strip()[:10], "%Y-%m-%d")
        days_es = ["lun", "mar", "mie", "jue", "vie", "sab", "dom"]
        return f"{days_es[dt.weekday()]} {dt.day}/{dt.month}"
    except (ValueError, IndexError):
        return time_str[:10]


def format_stops(outbound):
    """Format stops info: 'Directo' or '1 escala en City (Xh Xmin)'."""
    if outbound["num_stops"] == 0:
        return "Directo"

    city = outbound.get("layover_city", "")
    duration = outbound.get("layover_duration", 0)
    hours = duration // 60
    mins = duration % 60

    time_parts = []
    if hours > 0:
        time_parts.append(f"{hours}h")
    if mins > 0:
        time_parts.append(f"{mins}min")
    time_str = " ".join(time_parts) if time_parts else ""

    if city and time_str:
        return f"1 escala en {city} ({time_str})"
    elif city:
        return f"1 escala en {city}"
    return "1 escala"


# ── Message building ─────────────────────────────────────────────
LASTMINUTE_REMINDER = (
    "\U0001f4a1 Record\u00e1 chequear el mismo vuelo en lastminute.com\n"
    "para usar tu cr\u00e9dito de \u20ac300"
)


def build_alert_message(flights):
    """Build one Telegram message listing all matching flights,
    matching the old bot's emoji-per-line style."""
    cheapest_price = flights[0]["price"]

    # Header based on cheapest price
    if cheapest_price < URGENT_THRESHOLD:
        header = "\U0001f6a8 VUELO BARATO ENCONTRADO! \U0001f6a8"
    elif cheapest_price <= BUDGET_USD:
        header = "\u2705 BUEN PRECIO ENCONTRADO! \u2705"
    else:
        header = "\u2708\ufe0f UPDATE VUELOS EZE \u2192 PARIS \u2708\ufe0f"

    # Build a block for each flight
    flight_blocks = []
    for f in flights:
        ob = f["outbound"]
        stops_text = format_stops(ob)
        dep_full = ob["departure_time"]   # e.g. "2026-08-08 23:55"
        arr_full = ob["arrival_time"]     # e.g. "2026-08-09 17:50"

        block = (
            f"\u2708\ufe0f {ob['airline']} \u2022 {stops_text}\n"
            f"\U0001f570 Ida: {dep_full} \u2192 {arr_full}\n"
            f"\U0001f570 Vuelta: {RETURN_DATE}\n"
            f"\U0001f4b0 PRICE: ${f['price']} USD"
        )
        flight_blocks.append(block)

    # Google Flights search link
    search_link = (
        "https://www.google.com/travel/flights?q="
        f"Flights+EZE+to+Paris+{OUTBOUND_DATE}+return+{RETURN_DATE}"
    )

    parts = [header, ""]
    parts.extend("\n\n".join(flight_blocks).split("\n"))
    parts.append("")
    parts.append(f'\U0001f517 <a href="{search_link}">Book now on Google Flights</a>')
    parts.append("")
    parts.append(LASTMINUTE_REMINDER)

    return "\n".join(parts)


# ── CSV logging ──────────────────────────────────────────────────
def log_to_csv(flight):
    """Append the cheapest flight data to the CSV history file."""
    ob = flight["outbound"]
    file_exists = os.path.exists(CSV_FILE)

    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow([
                "date_checked", "airline", "outbound_departure",
                "outbound_arrival", "return_departure", "return_arrival",
                "stops", "price_usd",
            ])
        writer.writerow([
            datetime.now().strftime("%Y-%m-%d %H:%M"),
            ob["airline"],
            ob["departure_time"],
            ob["arrival_time"],
            "",  # return departure (not available from initial search)
            "",  # return arrival
            ob["num_stops"],
            flight["price"],
        ])


# ── Telegram ─────────────────────────────────────────────────────
def send_telegram(message):
    """Send a message to the Telegram chat."""
    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN environment variable not set")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    resp = requests.post(url, json=payload, timeout=15)
    resp.raise_for_status()
    result = resp.json()
    if not result.get("ok"):
        print(f"Telegram error: {result}")
    return result


# ── Scheduled check ──────────────────────────────────────────────
def check_flights():
    """Run a flight search, build the message, and send to Telegram."""
    now = datetime.now(BUE_TZ).strftime("%Y-%m-%d %H:%M %Z")
    print(f"\n[{now}] Running scheduled flight check...")

    try:
        data = search_flights()
        flights = parse_flights(data)

        if not flights:
            msg = (
                "\u2708\ufe0f Sin resultados para EZE \u2192 Paris (8-16 Ago)\n"
                "No se encontraron vuelos con los filtros actuales.\n"
                "Seguimos chequeando 4x/dia"
            )
            print("No flights match filters.")
            send_telegram(msg)
            return

        print(f"Found {len(flights)} flights. Cheapest: ${flights[0]['price']} USD")
        message = build_alert_message(flights)
        log_to_csv(flights[0])
        send_telegram(message)
        print("Sent to Telegram.")

    except Exception as e:
        print(f"Error during flight check: {e}")


# ── CLI support ──────────────────────────────────────────────────
def main():
    """Run a single check from the command line (for testing)."""
    dry_run = "--dry-run" in sys.argv

    print(f"Searching flights: {DEPARTURE_ID} -> {ARRIVAL_ID}")
    print(f"Dates: {OUTBOUND_DATE} -> {RETURN_DATE}")
    print(f"Budget: ${BUDGET_USD} USD")
    print()

    data = search_flights()
    flights = parse_flights(data)

    if not flights:
        msg = (
            "\u2708\ufe0f Sin resultados para EZE \u2192 Paris (8-16 Ago)\n"
            "No se encontraron vuelos con los filtros actuales.\n"
            "Seguimos chequeando 4x/dia"
        )
        print("No flights match filters.")
        print()
        print("Message:")
        print("-" * 40)
        print(msg)
        print("-" * 40)

        if not dry_run:
            send_telegram(msg)
            print("Sent to Telegram.")
        else:
            print("Dry run - not sending to Telegram.")
        return

    print(f"Found {len(flights)} flights matching filters.")
    print(f"Cheapest: ${flights[0]['price']} USD ({flights[0]['outbound']['airline']})")
    print()

    message = build_alert_message(flights)

    print("Message:")
    print("-" * 40)
    print(message)
    print("-" * 40)
    print()

    log_to_csv(flights[0])
    print(f"Logged to {CSV_FILE}")

    if dry_run:
        print("Dry run - not sending to Telegram.")
    else:
        send_telegram(message)
        print("Sent to Telegram.")


# ── Entry point ──────────────────────────────────────────────────
if __name__ == "__main__":
    # CLI mode: python tracker.py [--dry-run]
    if "--dry-run" in sys.argv or len(sys.argv) > 1:
        main()
    else:
        # Web service mode: start scheduler + Flask
        scheduler = BackgroundScheduler(timezone=BUE_TZ)

        # 4x/day: 8am, 11am, 3pm, 9pm Buenos Aires time
        scheduler.add_job(
            check_flights,
            CronTrigger(hour="8,11,15,21", minute=0, timezone=BUE_TZ),
            id="flight_check",
        )
        scheduler.start()
        print("Scheduler started: 4x/day at 8am, 11am, 3pm, 9pm ART")

        # Run one check immediately on startup
        check_flights()

        port = int(os.environ.get("PORT", 5000))
        app.run(host="0.0.0.0", port=port)
