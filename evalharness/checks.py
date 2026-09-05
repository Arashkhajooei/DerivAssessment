"""Rule-based, deterministic answer checks.

Each check is a small, independently callable function; `evaluate_answer`
composes them into one `AutomatedScoreRecord`. Adding a new check means
writing one function and wiring it into `evaluate_answer` -- nothing
elsewhere in the pipeline changes.

Nothing in this module references a specific query id, doc id, phrase, or
variant name: every check operates on whatever `Query` / answer text /
retrieved passages it is given.
"""

from __future__ import annotations

import re
from typing import Dict, List, Tuple

from evalharness.matching import match_any
from evalharness.retrieval import ScoredPassage
from evalharness.schemas import AutomatedScoreRecord, Query
from evalharness.text import normalize_tokens, tokenize

_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?%?")


def check_retrieval_hit(query: Query, retrieved: List[ScoredPassage]) -> bool:
    """True if any of the query's expected_doc_ids is among the retrieved
    passages. A query that declares no expected_doc_ids has nothing to
    check retrieval against -- default to True so it reads as "not
    applicable" rather than as a failure.
    """
    if not query.expected_doc_ids:
        return True
    retrieved_ids = {p.doc_id for p in retrieved}
    return any(doc_id in retrieved_ids for doc_id in query.expected_doc_ids)


def check_must_include(
    patterns: List[str], answer_text: str, max_gap: int
) -> Tuple[bool, List[str]]:
    """Pass if at least one required phrase/concept is present. An empty
    constraint list is vacuously satisfied -- there is nothing to require.
    Returns (passed, description-of-matches) for the notes field.
    """
    if not patterns:
        return True, []
    results = match_any(patterns, answer_text, max_gap=max_gap)
    matched = [
        "'{}' (via {})".format(p, r.rung) for p, r in zip(patterns, results) if r.matched
    ]
    return (len(matched) > 0), matched


def check_must_not_claim(
    patterns: List[str], answer_text: str, max_gap: int
) -> Tuple[bool, List[str]]:
    """Pass if none of the banned claims are present. An empty constraint
    list is vacuously satisfied.

    Known limitation, by design: this is lexical (subsequence/stemmed)
    matching, not negation-aware semantic understanding. A hedged or
    explicitly negated mention of a banned phrase can register as a match
    here. This layer is intentionally high-recall rather than
    high-precision -- the exact ambiguity the LLM review stage exists to
    resolve, and every violation this check raises is recorded (rung and
    matched text) so a human or the judge stage can see exactly what
    triggered it.
    """
    if not patterns:
        return True, []
    results = match_any(patterns, answer_text, max_gap=max_gap)
    violations = [
        "'{}' (via {})".format(p, r.rung) for p, r in zip(patterns, results) if r.matched
    ]
    return (len(violations) == 0), violations


def _find_quote_span(answer_tokens: List[str], evidence_text: str, min_span: int) -> bool:
    """True if a contiguous run of >= min_span raw answer tokens appears
    verbatim (case-insensitive, whitespace-joined) in the evidence text --
    a cheap, deterministic proxy for "the answer is quoting its source."
    """
    evidence_lower = evidence_text.lower()
    n = len(answer_tokens)
    if n < min_span:
        return False
    for start in range(n - min_span + 1):
        span = " ".join(answer_tokens[start : start + min_span])
        if span in evidence_lower:
            return True
    return False


def _unsupported_numbers(answer_text: str, evidence_text: str) -> List[str]:
    """Numbers/dates present in the answer but nowhere in the evidence
    text. A high-signal, nearly-free hallucination check: an invented "48
    hours" or "$500 fee" that appears nowhere in the retrieved passages is
    a strong grounding failure regardless of surrounding wording.
    """
    answer_numbers = set(_NUMBER_RE.findall(answer_text))
    evidence_lower = evidence_text.lower()
    return sorted(num for num in answer_numbers if num not in evidence_lower)


def compute_grounding_score(
    answer_text: str,
    retrieved: List[ScoredPassage],
    precision_weight: float,
    min_quote_span: int,
    quote_bonus: float,
    numeric_penalty: float,
    precision_decimals: int,
) -> Tuple[float, Dict[str, object]]:
    """Deterministic grounding proxy: token-overlap precision of the
    answer's content words against the retrieved evidence, with a bonus
    for verbatim quoting and a penalty for invented numbers/dates.

    This is a proxy for faithfulness, not a substitute for it -- it cannot
    detect a claim built entirely from words that individually appear in
    the evidence but combine into something the evidence never said. That
    gap is intentional: it is the boundary between what code should check
    and what the LLM judge stage is for.
    """
    evidence_text = " ".join("{} {}".format(p.title, p.text) for p in retrieved)

    answer_content_tokens = normalize_tokens(answer_text, drop_stopwords=True, apply_stem=True)
    evidence_tokens = set(normalize_tokens(evidence_text, drop_stopwords=True, apply_stem=True))

    if not answer_content_tokens:
        precision = 0.0
    else:
        overlap = sum(1 for t in answer_content_tokens if t in evidence_tokens)
        precision = overlap / len(answer_content_tokens)

    has_quote = _find_quote_span(tokenize(answer_text), evidence_text, min_quote_span)
    unsupported_numbers = _unsupported_numbers(answer_text, evidence_text)

    score = precision_weight * precision
    if has_quote:
        score += quote_bonus
    if unsupported_numbers:
        score -= numeric_penalty
    score = max(0.0, min(1.0, score))
    score = round(score, precision_decimals)

    details = {
        "precision": round(precision, precision_decimals),
        "has_quote_span": has_quote,
        "unsupported_numbers": unsupported_numbers,
    }
    return score, details


def evaluate_answer(
    query: Query,
    variant: str,
    answer_text: str,
    retrieved: List[ScoredPassage],
    config: dict,
) -> AutomatedScoreRecord:
    """Run every rule-based check for one (query, variant) answer and
    package the result into the automated_scores.json record shape.
    """
    matching_cfg = config["matching"]
    grounding_cfg = config["grounding"]
    max_gap = matching_cfg["max_gap"]

    retrieval_hit = check_retrieval_hit(query, retrieved)
    must_include_pass, matched_phrases = check_must_include(
        query.must_include_any, answer_text, max_gap
    )
    must_not_claim_pass, violated_phrases = check_must_not_claim(
        query.must_not_claim, answer_text, max_gap
    )
    grounding_score, grounding_details = compute_grounding_score(
        answer_text,
        retrieved,
        precision_weight=grounding_cfg["precision_weight"],
        min_quote_span=grounding_cfg["min_quote_span"],
        quote_bonus=grounding_cfg["quote_bonus"],
        numeric_penalty=grounding_cfg["numeric_penalty"],
        precision_decimals=grounding_cfg["score_precision"],
    )

    risk_flags: List[str] = []
    if not retrieval_hit:
        risk_flags.append("retrieval_miss")
    if not must_include_pass:
        risk_flags.append("missing_required_phrase")
    if not must_not_claim_pass:
        risk_flags.append("must_not_claim_violation")
    if grounding_details["unsupported_numbers"]:
        risk_flags.append("unsupported_numeric")
    if grounding_score < grounding_cfg["low_grounding_threshold"]:
        risk_flags.append("low_grounding")
    if not risk_flags:
        risk_flags = ["none"]

    notes = "; ".join(
        [
            "must_include: {}".format(
                "matched " + ", ".join(matched_phrases)
                if matched_phrases
                else ("n/a (no constraints)" if not query.must_include_any else "no match")
            ),
            "must_not_claim: {}".format(
                "VIOLATED " + ", ".join(violated_phrases)
                if violated_phrases
                else ("n/a (no constraints)" if not query.must_not_claim else "clean")
            ),
            "grounding: precision={}, quote_span={}, unsupported_numbers={}".format(
                grounding_details["precision"],
                grounding_details["has_quote_span"],
                grounding_details["unsupported_numbers"] or "none",
            ),
        ]
    )

    return AutomatedScoreRecord(
        query_id=query.query_id,
        variant=variant,
        retrieval_hit=retrieval_hit,
        must_include_pass=must_include_pass,
        must_not_claim_pass=must_not_claim_pass,
        grounding_score=grounding_score,
        risk_flags=risk_flags,
        notes=notes,
    )
