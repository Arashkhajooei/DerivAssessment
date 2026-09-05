# DerivAssessment — RAG Prompt Evaluation Harness

A replayable, offline evaluation pipeline for comparing two (or more)
prompting strategies for a retrieval-augmented support Q&A system. It
retrieves evidence deterministically, scores answers with a mix of
code-based checks and one controlled LLM judgment stage, detects safety and
grounding failures, and produces a promotion recommendation with stated
reasoning — not just a leaderboard.

This README documents the full design and will be extended as each stage
of the pipeline is built. **Status: retrieval, rule-based scoring, the LLM
review stage, failure taxonomy, and aggregation/recommendation are all
implemented and wired into `run.py`.** Remaining: the `validate.py`
artifact-consistency checker, the run manifest / provenance metadata, and
the `review_report.md` explainability view (a stretch goal).

## Quickstart (current state)

```bash
uv venv --python 3.12 .venv        # or: python3 -m venv .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/python run.py
```

The harness targets **Python 3.9+** for compatibility with older system
interpreters; it has been developed and tested on 3.12.

`python run.py` regenerates every implemented artifact:
`retrieval.json`, `automated_scores.json`, `llm_review.json`,
`llm_calls.jsonl`, `failure_taxonomy.json`, and `recommendation.md`.
`validate.py` is not implemented yet.

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
  text.py                    shared tokenizer, stemmer, stopword list
  matching.py                 the 3-rung phrase-matching ladder (exact ->
                              ordered-subsequence -> stemmed-subsequence)
  retrieval.py                deterministic BM25 index and search
  checks.py                    rule-based per-answer checks: retrieval_hit,
                              must_include/must_not_claim, grounding_score
  judge.py                    the one controlled LLM review stage:
                              prompt construction, structured-output
                              validation, and the cache/live/stub backends
  llm_log.py                   llm_calls.jsonl read/append helpers
  taxonomy.py                  the six-tag failure taxonomy, derived from
                              already-computed scores + judge output
  aggregate.py                  safety-gated aggregation and the
                              recommendation.md renderer

run.py                       orchestrates all five implemented stages

tests/
  test_loader.py              validation behaviour, incl. adversarial fixtures
  test_schemas.py              direct Pydantic model tests
  test_judge.py                 schema validation + backend resolution
                              (cache/live/stub), live backend mocked
  test_taxonomy.py              each failure tag's derivation rule
  test_aggregate.py             safety gate, tradeoff surfacing, k-variant
                              support, no-clear-winner cases
  fixtures/broken/             hand-built fixtures, each violating exactly
                                one rule, plus one fixture that must succeed
                                (3 variants, unconventional names, empty
                                constraint arrays) — proves fixture-swap
                                resilience, not just the happy path
```

Generated artifacts (`retrieval.json`, `automated_scores.json`,
`llm_review.json`, `llm_calls.jsonl`, `failure_taxonomy.json`,
`recommendation.md`) are written to the repository root by `run.py`,
matching the exact filenames the spec requires.

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

The reference screenshots providing `candidate_answers.json` initially
truncated two answer strings mid-sentence (`prompt_a` for Q1 and Q2). They
were completed by hand for the first commit and later replaced with the
full, authoritative text once it was provided. `candidate_answers.json`
now reflects the exact wording as given, with no edits. Since the harness
is schema-driven and swap-tolerant, the precise wording of the sample
answers was never load-bearing for the pipeline itself — this note exists
purely for a transparent history of the data.

## LLM review stage: enabling live judging

The judge stage resolves through three backends, in this order (see
`judge.backend_order` in `config.yaml`):

1. **cache** — replays a previously logged live call from `llm_calls.jsonl`
   by exact content hash (model + params + prompt + rubric version). Zero
   cost, fully offline, byte-identical output.
2. **live** — a real call to the Anthropic API, temperature 0, structured
   output enforced via tool use, validated in code before use.
3. **stub** — a deterministic, rule-derived judgment (no network, no
   model) used when no key is configured or the live call fails. The
   pipeline always completes; `llm_calls.jsonl` and `recommendation.md`
   both record which backend actually served each run so this is never
   silently mistaken for a real model judgment.

To use your own key:

```bash
pip install anthropic
export ANTHROPIC_API_KEY=sk-ant-...
python run.py
```

The first run makes one real API call (covering every query/answer pair)
and appends it to `llm_calls.jsonl`. Every subsequent run replays that
exact call from cache — free, offline, deterministic — until the inputs,
config, or prompt change, at which point the content hash changes and a
fresh live call is made.

**This repository was built and committed without an API key available.**
The live backend's request/response handling and structured-output
validation are implemented and unit-testable against a mocked API
response, but the live path itself has not been exercised against the
real Anthropic API in this environment. `llm_calls.jsonl` in this repo
therefore contains only `stub` entries. Testing the live path end-to-end
requires a key, which this environment does not have.

## Known limitations

- The rule-based `must_not_claim` / `must_include_any` matcher is lexical
  (exact → ordered-subsequence → stemmed-subsequence), not
  negation-aware. A hedged or explicitly negated mention of a banned
  phrase can register as a match; this is intentional (high recall over
  high precision at the deterministic layer) and is exactly the ambiguity
  the LLM judge stage is positioned to resolve.
- `grounding_score` is a token-overlap-plus-heuristics proxy for
  faithfulness, not a semantic entailment check. It cannot catch a claim
  built entirely from words that individually appear in the evidence but
  combine into something the evidence never said.
- The stub judge backend derives `winner`/`faithfulness`/`overclaim_flags`
  from the same deterministic signals already in `automated_scores.json`;
  it adds no independent judgment. `clarity` under the stub is a constant
  placeholder, not a real assessment — genuinely evaluating prose clarity
  requires the live LLM backend.
- BM25 retrieval, like any lexical retriever, is weakest on a query that
  shares few tokens with its answer passage (pure paraphrase). This
  harness has no embedding-based fallback by design (external calls for
  retrieval are disallowed by the brief).
- The aggregation/recommendation logic (`evalharness/aggregate.py`) is
  covered by unit tests on synthetic fixtures, but has not yet been
  reviewed end-to-end against a large, adversarial swapped fixture beyond
  the ones in `tests/fixtures/broken/`.
