# Slack Update Generator

Upload your weekly CSV export, verify the numbers, and copy a ready-to-paste Slack message.

## Setup

```bash
make setup
```

This creates a Python virtual environment and installs Flask.

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
3. The app auto-detects the last complete Monday–Sunday week
4. Review the status breakdown and verify the numbers
5. Use the dropdown to pick a different week if needed
6. Click "Copy to Clipboard" to grab the Slack message
7. Paste into Slack — replace the `X` in the contacts line with the real number

## CSV requirements

The CSV must contain these columns:
- `date` — in `M/DD/YYYY HH:MM:SS` format
- `status_account` — status values like "Enriched: Auto", "Needs Enrichment", "Junk", "Duplicate"
