.PHONY: run setup tracker tracker-dry tracker-serve

setup:
	python3 -m venv venv
	./venv/bin/pip install -r requirements.txt

run:
	PORT=5001 ./venv/bin/python app.py

tracker:
	./venv/bin/python tracker.py --dry-run

tracker-dry:
	./venv/bin/python tracker.py --dry-run

# Start tracker as a web service with scheduler (like Render does)
tracker-serve:
	PORT=5001 ./venv/bin/python tracker.py
