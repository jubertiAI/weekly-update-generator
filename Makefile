.PHONY: run setup

setup:
	python3 -m venv venv
	./venv/bin/pip install -r requirements.txt

run:
	PORT=5001 ./venv/bin/python app.py
