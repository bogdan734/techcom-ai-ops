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

# The review walkthrough: a clean run, a re-run that must send nothing, a run against
# a flaky API, and a model that answers garbage followed by its recovery. Every state
# directory must end with exactly 7 messages in its delivery log.
demo: clean
	@echo "\n=== 1. first run on the fixture"
	$(RUN) --out out
	@echo "\n=== 2. same data again: nothing may be sent twice"
	$(RUN) --out out
	@echo "\n=== 3. fresh state, flaky API: 429/503 on reads, a timeout after commit on send"
	$(RUN) --out out/flaky --fault tasks=429,503 --fault send=timeout-after-commit
	@echo "\n=== 4. fresh state, the model answers garbage: note-dependent tasks are held (exit 2)"
	-$(RUN) --out out/model --fault llm=garbagex50
	@echo "\n=== 5. the model is back: the held tasks go out, nothing twice"
	$(RUN) --out out/model
	@echo "\nmessages delivered: $$(wc -l < out/delivery_log.jsonl | tr -d ' ') in runs 1+2," \
		"$$(wc -l < out/flaky/delivery_log.jsonl | tr -d ' ') in run 3," \
		"$$(wc -l < out/model/delivery_log.jsonl | tr -d ' ') in runs 4+5"

clean:
	rm -rf out
