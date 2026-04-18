# Slack Update Generator

Upload your weekly CSV export, verify the numbers, and copy a ready-to-paste Slack message.

## Setup

```bash
make setup
```

This creates a Python virtual environment and installs dependencies (Flask, requests).

## Run

```bash
make run
```

The app starts at [http://localhost:5000](http://localhost:5000).

To use a different port, set the `PORT` environment variable:

```bash
PORT=8080 make run
```

## How to use

1. Open the app in your browser
2. Upload your CSV file (drag-and-drop or click to browse)
3. The app auto-detects the last complete Monday-Sunday week
4. Review the status breakdown and verify the numbers
5. Use the dropdown to pick a different week if needed
6. Click "Copy to Clipboard" to grab the Slack message
7. Paste into Slack - replace the `X` in the contacts line with the real number

## CSV requirements

The CSV must contain these columns:
- `date` - in `M/DD/YYYY HH:MM:SS` format
- `status_account` - status values like "Enriched: Auto", "Needs Enrichment", "Junk", "Duplicate"

---

# Flight Price Tracker (EZE -> Paris)

Monitors round-trip flight prices from Buenos Aires (EZE) to Paris (CDG/ORY/BVA) and sends alerts to a Telegram chat in Spanish.

## Search parameters

- **Route:** EZE -> CDG, ORY, or BVA (round trip)
- **Dates:** Aug 8, 2026 (outbound) - Aug 16, 2026 (return)
- **Budget:** $1,000 USD (~2.1M ARS)
- **Filters:** Outbound departs after 18:00, max 1 stop, layover under 4 hours
- **Baggage:** Carry-on only (default)

## Alert tiers

| Price | Alert type | Message |
|-------|-----------|---------|
| Under $900 | Urgent | Full flight details with savings amount |
| $900 - $1,000 | Good price | Full flight details with savings amount |
| Over $1,000 | Summary | Cheapest price found, difference from budget |

All messages are in Spanish and show prices in both USD and ARS.

## Environment variables

| Variable | Description |
|----------|-------------|
| `SERPAPI_KEY` | Your SerpApi API key |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token for sending messages |

## Run the tracker

```bash
# Live run (searches flights and sends Telegram message)
make tracker

# Dry run (searches flights, prints message, does NOT send to Telegram)
make tracker-dry
```

## Cron schedule

The tracker runs 4x/day at 8am, 11am, 3pm, and 9pm Buenos Aires time (UTC-3):

```
0 11,14,18,0 * * * cd /path/to/slack-update-generator && make tracker
```

## Price history

Each run logs the cheapest flight to `paris_price_history.csv` with columns:
`date_checked, airline, outbound_departure, outbound_arrival, return_departure, return_arrival, stops, price_usd`
