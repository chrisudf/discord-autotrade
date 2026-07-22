PY := .venv311/bin/python

.PHONY: test run venv

venv:
	/opt/homebrew/bin/python3.11 -m venv .venv311
	$(PY) -m pip install -q -r requirements.txt

test:
	$(PY) -m pytest tests/ -q

run:
	$(PY) -m autotrade.app.main
