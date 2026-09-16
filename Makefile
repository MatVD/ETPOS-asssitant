.PHONY: install dev test init-db create-user ingest

install:
	python3.12 -m venv .venv
	.venv/bin/python -m pip install --upgrade pip
	.venv/bin/python -m pip install -e '.[dev]'

dev:
	.venv/bin/uvicorn etpos_assistant.main:app --host 127.0.0.1 --port 8787 --reload

test:
	.venv/bin/pytest -q

init-db:
	.venv/bin/etpos-assistant init-db

create-user:
	.venv/bin/etpos-assistant create-user

ingest:
	.venv/bin/etpos-assistant ingest
