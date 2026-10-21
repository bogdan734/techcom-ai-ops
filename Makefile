PYTHON ?= python3
RUN = PYTHONPATH=src $(PYTHON) -m followup run --today 2026-10-21

.PHONY: install test lint run demo clean

install:
	$(PYTHON) -m pip install -r requirements-dev.txt
	$(PYTHON) -m pip install -e .

test:
	$(PYTHON) -m pytest

lint:
	ruff check src tests
	ruff format --check src tests

run:
	$(RUN) --out out

# The review walkthrough: a clean run, a re-run that must send nothing,
# and a run against a flaky API that must still send every message once.
demo: clean
	@echo "\n=== 1. first run on the fixture"
	$(RUN) --out out
	@echo "\n=== 2. same data again: nothing may be sent twice"
	$(RUN) --out out
	@echo "\n=== 3. fresh state, flaky API: 429/503 on reads, a timeout after commit on send"
	$(RUN) --out out/faults --fault tasks=429,503 --fault send=timeout-after-commit
	@echo "\nmessages delivered: $$(wc -l < out/delivery_log.jsonl | tr -d ' ') in runs 1+2," \
		"$$(wc -l < out/faults/delivery_log.jsonl | tr -d ' ') in run 3"

clean:
	rm -rf out
