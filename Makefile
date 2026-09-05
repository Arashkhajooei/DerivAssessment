# Convenience targets. Everything here is a thin wrapper around the two
# entrypoints the brief asks for: `python run.py` and `python validate.py`.

PYTHON ?= python3

.PHONY: help install run validate test clean

help:
	@echo "make install   Install runtime + dev dependencies"
	@echo "make run       Regenerate every derived artifact"
	@echo "make validate  Check the generated artifacts are complete and consistent"
	@echo "make test      Run the test suite"
	@echo "make clean     Remove generated artifacts"

install:
	$(PYTHON) -m pip install -r requirements-dev.txt

run:
	$(PYTHON) run.py

validate:
	$(PYTHON) validate.py

test:
	$(PYTHON) -m pytest -q

clean:
	rm -f retrieval.json automated_scores.json llm_review.json \
	      failure_taxonomy.json recommendation.md review_report.md \
	      run_manifest.json
	@echo "Note: llm_calls.jsonl is kept — it is the replay cache."
