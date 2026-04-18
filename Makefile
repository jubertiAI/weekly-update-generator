.PHONY: run setup tracker

setup:
	python3 -m venv venv
	./venv/bin/pip install -r requirements.txt

run:
	PORT=5001 ./venv/bin/python app.py

tracker:
	./venv/bin/python tracker.py

tracker-dry:
	./venv/bin/python tracker.py --dry-run

# Flight tracker cron: 4x/day at 8am, 11am, 3pm, 9pm Buenos Aires (UTC-3)
# = 11:00, 14:00, 18:00, 00:00 UTC
# 0 11,14,18,0 * * * cd /path/to/slack-update-generator && make tracker
