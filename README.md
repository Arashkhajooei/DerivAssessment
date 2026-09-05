# DerivAssessment — RAG Prompt Evaluation Harness

A replayable, offline evaluation pipeline for comparing two (or more)
prompting strategies for a retrieval-augmented support Q&A system. It
retrieves evidence deterministically, scores answers with a mix of
code-based checks and one controlled LLM judgment stage, detects safety and
grounding failures, and produces a promotion recommendation with stated
reasoning — not just a leaderboard.

**Status: complete.** All ten build steps are implemented: deterministic
retrieval, rule-based scoring, the one controlled LLM review stage
(cache/live/stub), the failure taxonomy, safety-gated aggregation and
recommendation, the explainability view, the run manifest, the
`validate.py` consistency checker, and the test suite (65 tests).

## Quickstart

```bash
uv venv --python 3.12 .venv        # or: python3 -m venv .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/python -m pytest -q      # 65 tests
.venv/bin/python run.py            # regenerate every artifact
.venv/bin/python validate.py       # check the artifacts are complete and consistent
```

Or, with `make`:

```bash
make install
make test
make run
make validate
```

The harness targets **Python 3.9+** for compatibility with older system
interpreters (verified with `py_compile` against the system 3.9
interpreter); it has been developed and tested on 3.12.

`run.py` regenerates every artifact: `retrieval.json`,
`automated_scores.json`, `llm_review.json`, `llm_calls.jsonl`,
`failure_taxonomy.json`, `recommendation.md`, `review_report.md`, and
`run_manifest.json`. `validate.py` checks that a completed run's
artifacts are present, well-formed, internally consistent, and that
`recommendation.md` is reproducible from the stored artifacts alone —
see "Validation" below for exactly what it checks.

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
  review_report.py            the per-query explainability view renderer
  manifest.py                  run manifest: git SHA, config hash, input
                              hashes, judge backend used, stage timings

run.py                       orchestrates all eight stages, writes every
                              artifact plus run_manifest.json
validate.py                   checks a completed run's artifacts for
                              presence, well-formedness, internal
                              consistency, and recommendation reproducibility

tests/
  test_loader.py              validation behaviour, incl. adversarial fixtures
  test_schemas.py              direct Pydantic model tests
  test_matching.py              the phrase-matching ladder, incl. every
                              near-miss case from the sample data and the
                              one case correctly out of lexical scope
  test_checks.py                grounding_score behavior: precision, quote
                              bonus, numeric penalty, empty constraints
  test_judge.py                 schema validation + backend resolution
                              (cache/live/stub), live backend mocked
  test_taxonomy.py              each failure tag's derivation rule
  test_aggregate.py             safety gate, tradeoff surfacing, k-variant
                              support, no-clear-winner cases
  test_determinism.py           repeated runs produce byte-identical
                              retrieval + scoring output
  test_pipeline_fixture_swap.py  the full pipeline (not just loading) run
                              end-to-end against a synthetic 3-variant,
                              empty-constraint fixture
  fixtures/broken/             hand-built fixtures, each violating exactly
                                one rule, plus one fixture that must succeed
                                (3 variants, unconventional names, empty
                                constraint arrays) — proves fixture-swap
                                resilience, not just the happy path
```

Generated artifacts (`retrieval.json`, `automated_scores.json`,
`llm_review.json`, `llm_calls.jsonl`, `failure_taxonomy.json`,
`recommendation.md`, `review_report.md`, `run_manifest.json`) are written
to the repository root by `run.py`, matching the exact filenames the spec
requires.

## Validation

`python validate.py` (or `make validate`) checks a completed run without
needing to re-run the pipeline:

1. every required artifact exists and is syntactically valid JSON/JSONL
   (Markdown files are checked for non-empty content)
2. every artifact's records conform to their Pydantic schema
3. every `(query_id, variant)` pair present in `candidate_answers.json`
   has a corresponding `automated_scores.json` record
4. `retrieval.json` contains exactly `config.retrieval.top_k` passages
   per query (or fewer only if the knowledge base itself has fewer
   documents than that)
5. every `llm_review.json` record uses only real variant names and
   in-range scores — this reuses the exact same `validate_reviews()`
   function the judge stage applies to a live API response, so there is
   one source of truth for "a valid review," not two implementations
   that could drift apart
6. `recommendation.md` is **recomputed from the stored artifacts**
   (`automated_scores.json`, `llm_review.json`, `failure_taxonomy.json`,
   plus the judge backend recorded in `run_manifest.json`) and diffed
   byte-for-byte against what's on disk — if a fixture or config changed
   since the last `run.py`, this fails loudly rather than silently
   serving a stale recommendation

Exit code 0 means every check passed; exit code 1 means at least one
failed, with every failure printed (not just the first). This was
verified by deliberately corrupting each of a stale `recommendation.md`,
a truncated `automated_scores.json`, and an invalid `winner` in
`llm_review.json` — each was caught with a specific, correct error
message, then the artifacts were restored.

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
- The composite score's weights (`config.yaml: aggregation.weights`) are
  a stated, documented modeling choice, not a discovered optimum. They
  are designed to be easy to audit and change, not to be treated as the
  one correct weighting — this is also called out live in every
  generated `recommendation.md`.
- With only 4 queries in the shipped sample fixture, any composite margin
  computed against it has limited statistical power; `recommendation.md`
  says so explicitly. A larger fixture would give the margin more
  meaning, but the harness itself places no lower bound on query count.
- `run_manifest.json`'s `git_commit` field is best-effort: outside a git
  checkout, or if the `git` binary is unavailable, it is recorded as
  `null` rather than failing the run.
