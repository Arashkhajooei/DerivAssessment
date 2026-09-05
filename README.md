# DerivAssessment — RAG Prompt Evaluation Harness

A replayable, offline evaluation pipeline for comparing two (or more)
prompting strategies for a retrieval-augmented support Q&A system. It
retrieves evidence deterministically, scores answers with a mix of
code-based checks and one controlled LLM judgment stage, detects safety and
grounding failures, and produces a promotion recommendation with stated
reasoning — not just a leaderboard.

This README documents the full design and will be extended as each stage of
the pipeline is built. **Status: inputs, config, and validated data loading
are complete (steps 0–1 of the build plan). Retrieval, scoring, the LLM
stage, aggregation, and the `run.py`/`validate.py` entrypoints are not yet
implemented.**

## Quickstart (current state)

```bash
uv venv --python 3.12 .venv        # or: python3 -m venv .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

The harness targets **Python 3.9+** for compatibility with older system
interpreters; it has been developed and tested on 3.12.

`python run.py` and `python validate.py` are not implemented yet — see
Status above.

## Repository layout

```
kb.json                    input: knowledge base passages
queries.json               input: user questions + answer constraints
candidate_answers.json     input: answers from each prompt variant
config.yaml                every threshold/weight, so behaviour changes
                            without touching code
requirements.txt           pinned runtime dependencies
requirements-dev.txt       + pytest, for running the test suite

evalharness/                 the library
  schemas.py                Pydantic models for every input and output file
  loader.py                  file loading + two-pass validation
                              (structural, then referential integrity)
  errors.py                  Issue/InputValidationError — all problems in
                              a fixture are collected and reported together
  config.py                  config.yaml loading + content hashing
  hashing.py                  file/text SHA-256 helpers (provenance, cache keys)

tests/
  test_loader.py              validation behaviour, incl. adversarial fixtures
  test_schemas.py              direct Pydantic model tests
  fixtures/broken/             hand-built fixtures, each violating exactly
                                one rule, plus one fixture that must succeed
                                (3 variants, unconventional names, empty
                                constraint arrays) — proves fixture-swap
                                resilience, not just the happy path
```

Generated artifacts (`retrieval.json`, `automated_scores.json`,
`llm_review.json`, `recommendation.md`, etc.) will land at the repository
root once the corresponding pipeline stage exists, matching the exact
filenames the spec requires.

## Design principles this repo follows

- **Schema, never data.** Nothing in `evalharness/` references a specific
  query id, KB doc id, phrase, or variant name. Variant names (`prompt_a`,
  `prompt_b`, ...) are read from `candidate_answers.json` as dictionary
  keys; the pipeline works unmodified for 1, 2, or N variants under any
  names. This is asserted by `tests/test_loader.py::TestFixtureSwapResilience`.
- **Fail fast, but report everything at once.** A malformed or
  inconsistent fixture raises `InputValidationError` before any pipeline
  stage runs, and that error lists every issue found — not just the first
  — so a swapped-in fixture with several problems doesn't need several
  rounds of fixing-and-rerunning to see them all.
- **Referential integrity is checked, not assumed.** The loader verifies
  that `expected_doc_ids` in `queries.json` actually resolve into
  `kb.json`, that every answer record references a real query, and that
  every query has a consistent set of answer variants. These are exactly
  the kind of cross-file bugs a hand-edited fixture can introduce silently.
- **Config over code.** Every tunable — BM25 parameters, matching
  tolerance, grounding score weights, risk severities, gate thresholds,
  aggregation weights, judge settings — lives in `config.yaml`. Its content
  hash will be recorded in the run manifest so a recommendation's exact
  settings are always traceable.

## A note on the sample answer data

The reference screenshots providing `candidate_answers.json` truncated
three answer strings mid-sentence (`prompt_a` for Q1 and Q3, cut off at
"...although some cases" and "...dated within" respectively). Those
strings were completed by hand to read naturally and stay consistent with
the corresponding `kb.json` passage:

- **Q1 / prompt_a**: "...although some cases may take longer if additional
  verification is required." (mirrors `D2`'s second sentence)
- **Q3 / prompt_a**: "...dated within the last 6 months." (mirrors `D3`'s
  accepted-documents sentence)

No other answer text was altered. Since the harness is designed to be
schema-driven and swap-tolerant, the exact wording of these sample answers
is not load-bearing for the pipeline itself — but it is documented here for
transparency, since it wasn't literally given.

## Known limitations (interim, will grow with the build)

- Retrieval, scoring, the LLM judge stage, aggregation/recommendation, and
  the failure taxonomy are not yet built. This section will be replaced
  with the harness's actual documented limitations once those stages land.
