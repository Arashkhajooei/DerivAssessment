"""The one controlled LLM review stage.

One batched call covers every query/answer pair in a single request. The
model is given the retrieved evidence, both (or all) candidate answers,
and the already-computed deterministic check results; it is asked only for
clarity, faithfulness, an overcommit flag, a per-question winner, and a
short justification -- never to retrieve evidence and never to produce the
final promotion recommendation (that is computed in `evalharness.aggregate`
from stored artifacts, per the brief's constraints).

Three backends resolve in the order given by `judge.backend_order` in
config.yaml:

    cache  replay a previously logged *live* call with an identical content
           hash (model + params + prompt + rubric version). Zero cost,
           fully offline, byte-identical output -- this is what makes a
           genuine LLM integration compatible with a "replayable" pipeline.
    live   a real Anthropic API call. Structured output is enforced via
           tool use (forced tool choice) and then re-validated in code
           (range checks, exact variant-key-set checks) before use --
           belt and suspenders, since a JSON-schema-conformant response can
           still contain an out-of-range score or an unknown variant name.
    stub   a deterministic, rule-derived judgment with no network call and
           no model. Always available, so the pipeline always completes
           even with no API key configured. Every field it produces is
           documented as derived, not judged -- see call_stub().

Every attempt (successful or not, on every backend) is appended to
llm_calls.jsonl, so it is always possible to tell after the fact which
backend actually served a given run's llm_review.json.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from evalharness.hashing import sha256_text
from evalharness.llm_log import append_call_log, read_call_log
from evalharness.loader import LoadedInputs
from evalharness.retrieval import ScoredPassage
from evalharness.schemas import AutomatedScoreRecord, LLMReviewRecord

RUBRIC_VERSION = "v1"

SYSTEM_PROMPT_TEMPLATE = """You are a strict, careful reviewer for a retrieval-augmented customer \
support system. For each of several questions you will be shown: the \
question, the evidence passages retrieved for it, and the answers produced \
by two or more candidate prompting strategies (the "variants"), along with \
results from deterministic rule-based checks already run on each answer.

Your ONLY job, for each question, is to judge:
  - clarity: how clear each answer is to a customer, an integer from \
{score_min} to {score_max}
  - faithfulness: how well each answer's claims are supported by the \
retrieved evidence, an integer from {score_min} to {score_max}
  - overclaim_flags: true/false per variant, whether that answer asserts \
something the retrieved evidence does not support
  - winner: the single variant name that is the better overall answer to \
that question
  - justification: 1-3 sentences explaining the winner choice, referencing \
the evidence

Rules you must follow:
  - Do not invent facts that are not present in the supplied evidence.
  - Do not perform retrieval or propose different evidence than what is given.
  - Do not produce any final deployment or promotion recommendation --only \
the per-question comparison described above.
  - Base every judgment strictly on the evidence and answers given to you.
  - "winner" must be exactly one of the variant names listed for that question.
  - Cover every question shown to you exactly once.

Respond by calling the submit_review tool exactly once, with your complete \
set of judgments for every question shown, and no other text."""

REVIEW_TOOL_SCHEMA = {
    "name": "submit_review",
    "description": "Submit the structured per-question comparative review.",
    "input_schema": {
        "type": "object",
        "properties": {
            "reviews": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "query_id": {"type": "string"},
                        "winner": {"type": "string"},
                        "faithfulness": {
                            "type": "object",
                            "additionalProperties": {"type": "integer"},
                        },
                        "clarity": {
                            "type": "object",
                            "additionalProperties": {"type": "integer"},
                        },
                        "overclaim_flags": {
                            "type": "object",
                            "additionalProperties": {"type": "boolean"},
                        },
                        "justification": {"type": "string"},
                    },
                    "required": [
                        "query_id",
                        "winner",
                        "faithfulness",
                        "clarity",
                        "overclaim_flags",
                        "justification",
                    ],
                },
            }
        },
        "required": ["reviews"],
    },
}


class LiveBackendUnavailable(Exception):
    """The live backend cannot even be attempted (package missing, no key).
    Distinct from LiveBackendError so the orchestrator can skip straight to
    the next configured backend without burning a retry budget.
    """


class LiveBackendError(Exception):
    """A live API call was attempted but failed (network, auth, rate
    limit, or an unexpected response shape).
    """


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_batch_prompt(
    inputs: LoadedInputs,
    retrieved_by_query: Dict[str, List[ScoredPassage]],
    automated_scores: List[AutomatedScoreRecord],
    variant_names_by_query: Dict[str, List[str]],
) -> str:
    """Build the single batched prompt covering every query/answer pair.
    Variant order within each question is sorted alphabetically so the
    prompt itself is deterministic regardless of any dict ordering.
    """
    by_key = {(r.query_id, r.variant): r for r in automated_scores}
    blocks: List[str] = []

    for query in inputs.queries:
        answer_set = inputs.answers_by_query.get(query.query_id)
        if answer_set is None:
            continue
        variants = variant_names_by_query.get(query.query_id, sorted(answer_set.answers))
        evidence = retrieved_by_query.get(query.query_id, [])
        evidence_lines = (
            "\n".join("  [{}] {}: {}".format(p.doc_id, p.title, p.text) for p in evidence)
            or "  (no evidence retrieved)"
        )

        answer_lines = []
        for v in variants:
            score = by_key.get((query.query_id, v))
            checks = (
                "retrieval_hit={}, must_include_pass={}, must_not_claim_pass={}, "
                "grounding_score={}, risk_flags={}".format(
                    score.retrieval_hit,
                    score.must_include_pass,
                    score.must_not_claim_pass,
                    score.grounding_score,
                    score.risk_flags,
                )
                if score is not None
                else "no deterministic checks available"
            )
            answer_lines.append(
                '  variant "{}":\n'
                '    answer: "{}"\n'
                "    deterministic checks: {}".format(v, answer_set.answers[v], checks)
            )

        blocks.append(
            'Question {}: "{}"\n'
            "Retrieved evidence:\n{}\n"
            "Candidate answers:\n{}\n"
            "Variant names for this question: {}".format(
                query.query_id,
                query.user_question,
                evidence_lines,
                "\n".join(answer_lines),
                variants,
            )
        )

    return "\n\n---\n\n".join(blocks)


def compute_prompt_hash(model: str, temperature: float, max_tokens: int, prompt: str) -> str:
    """Content-address the exact call this batch represents. If the model,
    sampling params, rubric version, or prompt text change in any way, the
    hash changes and the cache correctly treats it as a new call rather
    than incorrectly replaying a stale one.
    """
    canonical = "model={}|temperature={}|max_tokens={}|rubric={}|prompt={}".format(
        model, temperature, max_tokens, RUBRIC_VERSION, prompt
    )
    return sha256_text(canonical)


def call_live(system_prompt: str, user_prompt: str, config: dict) -> Tuple[str, int, Optional[dict]]:
    """One real call to the Anthropic API with structured output enforced
    via forced tool use. Returns (raw_json_text, latency_ms, usage_dict).

    Raises LiveBackendUnavailable if the optional `anthropic` package is
    not installed or no API key is configured (caller should fall through
    to the next backend without spending a retry). Raises LiveBackendError
    if a call was attempted but failed or returned an unusable shape.
    """
    try:
        import anthropic
    except ImportError as exc:
        raise LiveBackendUnavailable("the 'anthropic' package is not installed") from exc

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise LiveBackendUnavailable("ANTHROPIC_API_KEY is not set")

    judge_cfg = config["judge"]
    client = anthropic.Anthropic(api_key=api_key)

    start = time.monotonic()
    try:
        response = client.messages.create(
            model=judge_cfg["model"],
            max_tokens=judge_cfg["max_tokens"],
            temperature=judge_cfg["temperature"],
            system=system_prompt,
            tools=[REVIEW_TOOL_SCHEMA],
            tool_choice={"type": "tool", "name": "submit_review"},
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception as exc:  # noqa: BLE001 -- any SDK/network/auth failure
        raise LiveBackendError(str(exc)) from exc
    latency_ms = int((time.monotonic() - start) * 1000)

    tool_blocks = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
    if not tool_blocks:
        raise LiveBackendError("model response did not include a tool_use block")

    raw_json_text = json.dumps(tool_blocks[0].input, sort_keys=True)
    usage = getattr(response, "usage", None)
    usage_dict = None
    if usage is not None:
        usage_dict = {
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
        }
    return raw_json_text, latency_ms, usage_dict


def validate_reviews(
    parsed: object, inputs: LoadedInputs, score_min: int, score_max: int
) -> Tuple[List[LLMReviewRecord], List[str]]:
    """Validate a parsed {"reviews": [...]} payload against the schema and
    every constraint the JSON-schema tool definition alone cannot express:
    winner must be an actual variant for that question, faithfulness/
    clarity/overclaim_flags keys must exactly equal that question's
    variant set (supports any k), scores must fall in the configured
    range, and every query that has answers must be covered exactly once.

    Returns (records, errors). Non-empty errors means the whole batch is
    rejected -- there is no partial acceptance, so a caller either has a
    fully valid review or falls through to the next backend.
    """
    errors: List[str] = []
    if not isinstance(parsed, dict) or not isinstance(parsed.get("reviews"), list):
        return [], ["response must be an object with a 'reviews' array"]

    by_query_id: Dict[str, LLMReviewRecord] = {}
    for entry in parsed["reviews"]:
        if not isinstance(entry, dict):
            errors.append("a review entry is not a JSON object")
            continue

        qid = entry.get("query_id")
        if qid not in inputs.queries_by_id:
            errors.append("unknown query_id '{}'".format(qid))
            continue
        if qid in by_query_id:
            errors.append("duplicate review for query_id '{}'".format(qid))
            continue

        answer_set = inputs.answers_by_query.get(qid)
        variant_set = set(answer_set.answers) if answer_set else set()

        winner = entry.get("winner")
        if winner not in variant_set:
            errors.append(
                "query_id '{}': winner '{}' is not in the variant set {}".format(
                    qid, winner, sorted(variant_set)
                )
            )
            continue

        ok = True
        typed_fields = {}
        for field_name, is_bool in (
            ("faithfulness", False),
            ("clarity", False),
            ("overclaim_flags", True),
        ):
            value = entry.get(field_name)
            if not isinstance(value, dict) or set(value) != variant_set:
                errors.append(
                    "query_id '{}': '{}' keys must exactly equal variant set {}".format(
                        qid, field_name, sorted(variant_set)
                    )
                )
                ok = False
                continue
            for variant_name, v in value.items():
                if is_bool:
                    if not isinstance(v, bool):
                        errors.append(
                            "query_id '{}': {}['{}'] must be a boolean".format(
                                qid, field_name, variant_name
                            )
                        )
                        ok = False
                else:
                    if not isinstance(v, int) or isinstance(v, bool) or not (
                        score_min <= v <= score_max
                    ):
                        errors.append(
                            "query_id '{}': {}['{}'] must be an integer in [{}, {}]".format(
                                qid, field_name, variant_name, score_min, score_max
                            )
                        )
                        ok = False
            typed_fields[field_name] = value

        justification = entry.get("justification")
        if not isinstance(justification, str) or not justification.strip():
            errors.append("query_id '{}': justification must be a non-empty string".format(qid))
            ok = False

        if not ok:
            continue

        by_query_id[qid] = LLMReviewRecord(
            query_id=qid,
            winner=winner,
            faithfulness=typed_fields["faithfulness"],
            clarity=typed_fields["clarity"],
            overclaim_flags=typed_fields["overclaim_flags"],
            justification=justification.strip(),
        )

    expected_ids = [q.query_id for q in inputs.queries if q.query_id in inputs.answers_by_query]
    missing = [qid for qid in expected_ids if qid not in by_query_id]
    if missing:
        errors.append("missing review(s) for query_id(s): {}".format(missing))

    if errors:
        return [], errors
    return [by_query_id[qid] for qid in expected_ids], []


def _pick_stub_winner(variants: List[str], scores: Dict[str, Optional[AutomatedScoreRecord]]) -> str:
    """Deterministic winner selection for the stub backend: prefer no
    banned-claim violation, then a satisfied must_include constraint, then
    higher grounding_score, tie-broken by ascending variant name so the
    result never depends on dict/list ordering.
    """

    def rank(v: str) -> Tuple[int, int, float]:
        s = scores.get(v)
        if s is None:
            return (0, 0, 0.0)
        return (int(s.must_not_claim_pass), int(s.must_include_pass), s.grounding_score)

    best = max(rank(v) for v in variants)
    tied = sorted(v for v in variants if rank(v) == best)
    return tied[0]


def call_stub(
    inputs: LoadedInputs, automated_scores: List[AutomatedScoreRecord], config: dict
) -> List[LLMReviewRecord]:
    """Deterministic, rule-derived judgment used when no live LLM backend
    is available. Every field is explicitly derived from the already
    -computed deterministic checks -- this is not an attempt to imitate a
    real model judgment, and the justification text says so plainly so it
    is never mistaken for one when read in llm_review.json.
    """
    judge_cfg = config["judge"]
    score_min, score_max = judge_cfg["score_min"], judge_cfg["score_max"]
    by_key = {(r.query_id, r.variant): r for r in automated_scores}

    records: List[LLMReviewRecord] = []
    for query in inputs.queries:
        answer_set = inputs.answers_by_query.get(query.query_id)
        if answer_set is None:
            continue
        variants = sorted(answer_set.answers)
        scores = {v: by_key.get((query.query_id, v)) for v in variants}

        winner = _pick_stub_winner(variants, scores)
        faithfulness, clarity, overclaim_flags = {}, {}, {}
        for v in variants:
            s = scores[v]
            if s is None:
                faithfulness[v] = score_min
                clarity[v] = score_min
                overclaim_flags[v] = True
                continue
            faithfulness[v] = round(score_min + s.grounding_score * (score_max - score_min))
            clarity[v] = (score_min + score_max) // 2  # placeholder, see README limitations
            overclaim_flags[v] = (not s.must_not_claim_pass) or (
                "unsupported_numeric" in s.risk_flags
            )

        justification = (
            "Stub judge (no live LLM backend available): winner derived from "
            "deterministic checks in priority order (must_not_claim_pass, "
            "must_include_pass, grounding_score; ties broken alphabetically). "
            "faithfulness is grounding_score rescaled to [{}, {}]; clarity is a "
            "constant placeholder, not independently assessed; overclaim_flags "
            "reflect must_not_claim_pass and the unsupported_numeric risk flag.".format(
                score_min, score_max
            )
        )

        records.append(
            LLMReviewRecord(
                query_id=query.query_id,
                winner=winner,
                faithfulness=faithfulness,
                clarity=clarity,
                overclaim_flags=overclaim_flags,
                justification=justification,
            )
        )
    return records


def _repair_prompt(original_prompt: str, errors: List[str]) -> str:
    return (
        original_prompt
        + "\n\n---\n\nA previous response failed validation with these problems:\n"
        + "\n".join("- {}".format(e) for e in errors)
        + "\n\nCall submit_review again with a fully corrected response that fixes "
        "every problem above."
    )


def run_judge_stage(
    inputs: LoadedInputs,
    retrieved_by_query: Dict[str, List[ScoredPassage]],
    automated_scores: List[AutomatedScoreRecord],
    config: dict,
) -> Tuple[List[LLMReviewRecord], str]:
    """Resolve the judge through config.judge.backend_order and return
    (records, backend_used). Every attempt on every backend is appended to
    llm_calls.jsonl before this function returns.
    """
    judge_cfg = config["judge"]
    variant_names_by_query = {
        q.query_id: sorted(inputs.answers_by_query[q.query_id].answers)
        for q in inputs.queries
        if q.query_id in inputs.answers_by_query
    }
    prompt = build_batch_prompt(inputs, retrieved_by_query, automated_scores, variant_names_by_query)
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        score_min=judge_cfg["score_min"], score_max=judge_cfg["score_max"]
    )
    prompt_hash = compute_prompt_hash(
        judge_cfg["model"], judge_cfg["temperature"], judge_cfg["max_tokens"], system_prompt + prompt
    )

    log_path = config["paths"]["llm_calls"]
    existing_log = read_call_log(log_path)
    new_entries: List[dict] = []

    for backend in judge_cfg["backend_order"]:
        if backend == "cache":
            hit = next(
                (
                    e
                    for e in reversed(existing_log)
                    if e.get("prompt_hash") == prompt_hash
                    and e.get("backend") == "live"
                    and e.get("parsed_ok")
                ),
                None,
            )
            if hit is None:
                continue
            try:
                parsed = json.loads(hit.get("response_raw") or "")
            except json.JSONDecodeError:
                continue
            records, errors = validate_reviews(
                parsed, inputs, judge_cfg["score_min"], judge_cfg["score_max"]
            )
            if errors:
                continue
            new_entries.append(
                {
                    "call_id": str(uuid.uuid4()),
                    "timestamp": _now_iso(),
                    "backend": "cache",
                    "replayed_call_id": hit.get("call_id"),
                    "model": judge_cfg["model"],
                    "prompt_hash": prompt_hash,
                    "parsed_ok": True,
                    "validation_errors": [],
                }
            )
            append_call_log(log_path, new_entries)
            return records, "cache"

        elif backend == "live":
            attempt_prompt = prompt
            unavailable = False
            for _attempt in range(judge_cfg["max_retries"] + 1):
                try:
                    raw_json_text, latency_ms, usage = call_live(
                        system_prompt, attempt_prompt, config
                    )
                except LiveBackendUnavailable as exc:
                    new_entries.append(
                        {
                            "call_id": str(uuid.uuid4()),
                            "timestamp": _now_iso(),
                            "backend": "live",
                            "model": judge_cfg["model"],
                            "prompt_hash": prompt_hash,
                            "parsed_ok": False,
                            "validation_errors": ["unavailable: {}".format(exc)],
                        }
                    )
                    unavailable = True
                    break
                except LiveBackendError as exc:
                    new_entries.append(
                        {
                            "call_id": str(uuid.uuid4()),
                            "timestamp": _now_iso(),
                            "backend": "live",
                            "model": judge_cfg["model"],
                            "prompt_hash": prompt_hash,
                            "parsed_ok": False,
                            "validation_errors": ["live call failed: {}".format(exc)],
                        }
                    )
                    continue

                try:
                    parsed = json.loads(raw_json_text)
                except json.JSONDecodeError as exc:
                    new_entries.append(
                        {
                            "call_id": str(uuid.uuid4()),
                            "timestamp": _now_iso(),
                            "backend": "live",
                            "model": judge_cfg["model"],
                            "prompt_hash": prompt_hash,
                            "response_raw": raw_json_text,
                            "latency_ms": latency_ms,
                            "usage": usage,
                            "parsed_ok": False,
                            "validation_errors": ["invalid JSON: {}".format(exc)],
                        }
                    )
                    attempt_prompt = _repair_prompt(prompt, ["response was not valid JSON"])
                    continue

                records, errors = validate_reviews(
                    parsed, inputs, judge_cfg["score_min"], judge_cfg["score_max"]
                )
                if errors:
                    new_entries.append(
                        {
                            "call_id": str(uuid.uuid4()),
                            "timestamp": _now_iso(),
                            "backend": "live",
                            "model": judge_cfg["model"],
                            "prompt_hash": prompt_hash,
                            "response_raw": raw_json_text,
                            "latency_ms": latency_ms,
                            "usage": usage,
                            "parsed_ok": False,
                            "validation_errors": errors,
                        }
                    )
                    attempt_prompt = _repair_prompt(prompt, errors)
                    continue

                new_entries.append(
                    {
                        "call_id": str(uuid.uuid4()),
                        "timestamp": _now_iso(),
                        "backend": "live",
                        "model": judge_cfg["model"],
                        "prompt_hash": prompt_hash,
                        "response_raw": raw_json_text,
                        "latency_ms": latency_ms,
                        "usage": usage,
                        "parsed_ok": True,
                        "validation_errors": [],
                    }
                )
                append_call_log(log_path, new_entries)
                return records, "live"

            del unavailable  # retries (or unavailability) exhausted; fall through
            continue

        elif backend == "stub":
            records = call_stub(inputs, automated_scores, config)
            new_entries.append(
                {
                    "call_id": str(uuid.uuid4()),
                    "timestamp": _now_iso(),
                    "backend": "stub",
                    "model": None,
                    "prompt_hash": prompt_hash,
                    "parsed_ok": True,
                    "validation_errors": [],
                }
            )
            append_call_log(log_path, new_entries)
            return records, "stub"

    append_call_log(log_path, new_entries)
    raise RuntimeError(
        "no judge backend produced a valid result; check judge.backend_order in config.yaml "
        "(it should end in 'stub' so the pipeline always completes)"
    )
