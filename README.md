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

## Contents

- [Quickstart](#quickstart)
- [Repository layout](#repository-layout)
- [Artifacts reference](#artifacts-reference) — what's in each generated file
- [How the recommendation is computed](#how-the-recommendation-is-computed) — the aggregation rule, plain English
- [LLM review stage: enabling live judging](#llm-review-stage-enabling-live-judging)
- [Validation](#validation)
- [Design principles this repo follows](#design-principles-this-repo-follows)
- [Design tradeoffs and why](#design-tradeoffs-and-why)
- [Known limitations](#known-limitations)
- [What I'd add next](#what-id-add-next)
- [A note on the sample answer data](#a-note-on-the-sample-answer-data)

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

run.py                       orchestrates the full pipeline end to end
                              (retrieval -> scoring -> LLM review ->
                              taxonomy -> recommendation -> explainability
                              view -> manifest) and writes all 8 output files
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

## Artifacts reference

What each generated file actually contains, one record shape at a time.

**`retrieval.json`** — one record per query: `{query_id, retrieved: [{doc_id, score, title, text}]}`. `retrieved` holds up to `config.retrieval.top_k` passages (fewer only if the knowledge base itself has fewer documents), highest BM25 score first, ties broken by ascending `doc_id`.

**`automated_scores.json`** — one record per `(query_id, variant)`: `{query_id, variant, retrieval_hit, must_include_pass, must_not_claim_pass, grounding_score, risk_flags, notes}`. Every field here comes from deterministic code, never the LLM. `risk_flags` is `["none"]` when nothing fired, otherwise any of `retrieval_miss`, `missing_required_phrase`, `must_not_claim_violation`, `unsupported_numeric`, `low_grounding`.

**`llm_review.json`** — one record per query: `{query_id, winner, faithfulness: {variant: int}, clarity: {variant: int}, overclaim_flags: {variant: bool}, justification}`. The three dicts are keyed by whatever variant names exist for that question — 2 or 20 — never a fixed pair of fields. `winner` is always one of those variant names.

**`llm_calls.jsonl`** — one line per backend *attempt* (not per successful call): `{call_id, timestamp, backend, model, prompt_hash, parsed_ok, validation_errors, response_raw?, latency_ms?, usage?}`. A single `run.py` invocation can append several lines (e.g. a failed `live` attempt followed by a successful `stub` fallback) — this is an append-only audit log, read it in full to see everything that was tried, not just the last line.

**`failure_taxonomy.json`** — one record per `(query_id, variant)`: `{query_id, variant, tags}`, where `tags` is zero or more of the six fixed values named in the brief (`unsupported_claim`, `missed_key_fact`, `policy_violation`, `retrieval_miss`, `overconfident_tone`, `irrelevant_answer`).

**`recommendation.md`** — human-readable: selected variant, top reasons, tradeoffs (only rendered if any were found), a per-variant summary table (composite score, mean judge faithfulness/clarity, judge win count, failure tag counts), a per-query detail table, and a "known limitations of this run" section that names which judge backend actually served it.

**`review_report.md`** — the explainability view: for every query, the question, retrieved evidence with scores, every variant's answer text, which rule checks it failed, its failure tags, and the judge's winner for that question — everything needed to debug one query without cross-referencing four JSON files by hand.

**`run_manifest.json`** — provenance: `generated_at`, `git_commit` (best-effort, `null` outside a git checkout), `config_hash`, `input_hashes` (SHA-256 of each of the 3 input files), `python_version`, `judge.{model, backend_order, backend_used}`, `counts`, and `stage_timings_seconds`.

## How the recommendation is computed

The full rule, in order, exactly as implemented in `evalharness/aggregate.py`:

1. **Safety gate (a veto, not a penalty).** For every variant, walk every query it answered. If `must_not_claim_pass` is `false` on a query whose `risk_level` meets or exceeds `config.gates.disqualify_must_not_claim_at_severity` (default: `high` and above), that variant is **disqualified outright** — no composite score, however high, can buy it back. This is deliberate: a variant that is clearer, better-grounded, and wins every other query but tells one customer their password can be emailed to them must never be promotable.

2. **Composite score, for every variant (including disqualified ones).** Per query, a weighted blend of `retrieval_hit`, `must_include_pass`, `must_not_claim_pass`, `grounding_score`, and the judge's `faithfulness`/`clarity`/winner-vote (weights in `config.aggregation.weights`), averaged across all queries that variant answered. Disqualified variants still get a composite — it's what lets the report show *how good it otherwise was*, which is exactly what the tradeoff-surfacing step below needs.

3. **Pick among the survivors.**
   - Zero variants pass the gate → **no promotion recommended**, reasons listed.
   - Exactly one passes → **it is promoted**, no further comparison needed.
   - More than one passes → the highest composite score wins, but only if its margin over the runner-up is at least `config.aggregation.min_margin_for_promotion` (default `0.02`). Below that margin, the evidence is treated as **too thin to prefer one over the other** — the harness explicitly recommends no promotion rather than picking a coin-flip winner and dressing it up as a finding.

4. **Surface the tradeoff, automatically.** If any disqualified variant's mean judge-rated clarity is higher than the promoted (or best surviving) variant's, that is called out by name in `recommendation.md`'s "Tradeoffs" section — the literal case the brief asks for: "explain tradeoffs if one variant is clearer but less safe."

Every one of these four steps reads only already-written artifacts (`automated_scores.json`, `llm_review.json`, `failure_taxonomy.json`) plus `config.yaml` — nothing here re-touches raw answer text or calls the judge again, which is what makes `validate.py`'s reproducibility check (below) meaningful rather than circular.

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
  hash is recorded in `run_manifest.json` (alongside the git commit and
  every input file's SHA-256) so a recommendation's exact settings are
  always traceable after the fact, not just at the moment it was made.

## Design tradeoffs and why

Concrete decisions made during the build, and the reasoning behind each —
distinct from "Known limitations" below, which lists the *consequences*
of these choices rather than the choices themselves.

- **BM25 over embeddings, for retrieval.** Deterministic, fully local, no
  model download, no network call — and the brief explicitly disallows
  external calls for retrieval. The tradeoff: BM25 is weak on a query
  that shares few tokens with its answer passage (pure paraphrase). No
  embedding-based fallback is included; see "What I'd add next."
- **A 3-rung lexical matching ladder, not pure substring or pure semantic
  matching.** Free, instant, and fully auditable — the rung that matched
  is recorded in every check's notes. It resolves the majority of lexical
  drift seen in the actual sample data (an inserted word, a plural/verb
  form, a reworded copula) without a model call. The one case that is
  genuinely semantic-only (no shared tokens at all) is deliberately left
  unmatched by design, not patched around — that gap is exactly what the
  LLM judge stage exists to close.
- **One batched LLM call across every query/answer pair, not one call per
  answer.** This is what the brief asks for, and it has real
  consequences: latency and cost are bounded and predictable, and the
  pipeline's only non-deterministic seam is a single, cacheable call
  rather than scattered throughout. The cost is atomicity — if any part
  of the batch response fails validation, the *entire* batch is rejected
  and retried/falls back, rather than salvaging the queries that parsed
  correctly. Chosen deliberately: partial acceptance would mean a
  recommendation could silently mix judged and un-judged queries with no
  visible marker of which was which.
- **Structured output enforced via forced tool use, then re-validated in
  code anyway.** A JSON-schema tool definition can require "an object
  with these keys" but cannot express "an integer between 1 and 5" or
  "these dict keys must exactly equal this question's variant set,
  whatever that set happens to be." Code-side validation is not
  redundant with the API's structured-output guarantee — it checks
  things the schema mechanically cannot.
- **The safety gate is a hard veto, not a weighted term in the composite
  score.** A weighted-penalty design was considered and rejected: with
  large enough weights on faithfulness/clarity, a strong-enough advantage
  there could always mathematically outvote a real safety violation for
  some input. A veto has no such failure mode — no composite score,
  however high, can un-disqualify a variant that told a customer their
  password could be emailed to them.
- **Backend order `cache -> live -> stub`, and a stub that says plainly
  it is not a real judgment.** A live-only design would fail closed for
  any evaluator without an API key, on a task that explicitly says "if no
  API key is available, your pipeline should still run ... or provide a
  stubbed path." A fully mocked judge would dodge the requirement to
  build a real integration at all. This design does both: a genuine
  Anthropic tool-use integration exists and is unit-tested, and the
  pipeline still completes with zero configuration.
- **A single winner-takes-one recommendation, with an explicit "no
  promotion" outcome — not a leaderboard or a forced pick.** The brief
  asks for a recommendation, not a report a human still has to interpret.
  Refusing to promote when the margin is thin is a deliberate stance
  against manufacturing false confidence from noisy per-query signals.
- **Pydantic `extra="forbid"` on every output model, `extra="allow"` on
  every input model.** Outputs are this harness's own contract and should
  be exact. Inputs may legitimately carry fields an evaluator's fixture
  uses for its own bookkeeping that this harness doesn't need — rejecting
  those outright would be needless brittleness in the direction that
  matters least.
- **Every threshold lives in `config.yaml`, none in code.** BM25
  parameters, the matching gap budget, grounding weights, risk
  severities, the disqualifying severity, aggregation weights, the
  promotion margin, judge settings — all of it. This is what makes "a
  sensible tradeoff" auditable after the fact rather than an implicit
  assumption buried in a function: the config's content hash is in every
  run's manifest, so the exact settings behind any past recommendation
  can always be recovered.

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

## What I'd add next

Roughly in the order I'd tackle them:

1. **Actually exercise the live judge backend against the real Anthropic
   API.** Everything downstream of "a response came back" is unit-tested
   against a mocked call (10 tests in `test_judge.py`), but the live path
   itself has never made a real request in this environment — no key was
   available. I'd add a small integration test that runs only when
   `ANTHROPIC_API_KEY` is set (skipped otherwise) so the real path gets
   continuous coverage the moment a key exists.
2. **A negation-aware check to close the lexical ladder's most-cited
   gap.** The matcher can register a hedged or explicitly negated mention
   of a banned phrase as a violation. A small local NLI model or a
   negation-scope heuristic over the matched span would tighten this
   without giving up the ladder's speed and auditability for the common
   case.
3. **Statistical rigor on the promotion margin.** The current check is a
   single fixed threshold (`min_margin_for_promotion`). A paired
   bootstrap confidence interval over the per-query composite
   differences would let "no promotion" be backed by a stated confidence
   level instead of an arbitrary cutoff — much more defensible at a
   larger query count than the 4 in the shipped fixture.
4. **An embedding-based retrieval fallback**, still fully local (e.g. a
   small sentence-transformer with no network call at inference time),
   for the subset of queries where BM25's token overlap is weak. Alongside
   it: report recall@k / MRR as a retrieval-quality health diagnostic in
   its own right, not just as an input to `retrieval_hit`.
5. **CI.** A GitHub Actions workflow running `pytest`, `python run.py`,
   and `python validate.py` on every push, so "runs from a clean
   checkout" is continuously verified rather than something I confirmed
   manually once before handing it over.
6. **A cost/latency rollup across runs.** `llm_calls.jsonl` already logs
   token usage and latency per call; nothing yet aggregates it into a
   $-and-seconds summary across many runs. That's a small addition on top
   of data the harness already collects.
7. **A pairwise judge mode as a config-toggleable alternative** to the
   current pointwise-with-winner design, with order-swap bias correction
   — for teams that want the extra sensitivity a head-to-head comparison
   gives and are willing to pay for the added complexity of correcting
   for position bias.
8. **A multi-model judge ensemble** (two different judge models voting)
   behind the same batching/caching machinery, to reduce how much the one
   genuinely non-deterministic stage depends on a single model's
   idiosyncrasies.

## A note on the sample answer data

The reference screenshots providing `candidate_answers.json` initially
truncated two answer strings mid-sentence (`prompt_a` for Q1 and Q2). They
were completed by hand for the first commit and later replaced with the
full, authoritative text once it was provided. `candidate_answers.json`
now reflects the exact wording as given, with no edits. Since the harness
is schema-driven and swap-tolerant, the precise wording of the sample
answers was never load-bearing for the pipeline itself — this note exists
purely for a transparent history of the data.

