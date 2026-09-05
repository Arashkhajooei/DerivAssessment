"""Failure taxonomy: a small, fixed controlled vocabulary of failure
modes, with zero or more tags assigned per (query, variant) answer.

Every tag is derived deterministically from already-computed artifacts
(automated_scores.json fields and, where available, the LLM judge's
overclaim_flags) plus one lightweight lexical check. Nothing here
re-derives grounding or matching from scratch -- this stage is purely a
relabeling of signals the earlier stages already computed, which is what
keeps it cheap to extend: a new tag is a new rule over existing fields.

TAGS (fixed, matches the vocabulary named in the brief):
    unsupported_claim   the answer asserts something the evidence does not
                        support: a banned claim was made, a number/date in
                        the answer appears nowhere in the evidence, or the
                        judge itself flagged an overclaim
    missed_key_fact     the answer omits a fact the query says it must
                        include (must_include_any had no match)
    policy_violation    a banned claim was made on a query whose
                        risk_level meets the promotion-disqualifying
                        severity threshold (config.gates) -- the subset of
                        unsupported_claim failures serious enough to gate
                        promotion outright
    retrieval_miss      none of the query's expected evidence was
                        retrieved for this question
    overconfident_tone  the answer uses an absolute/confidence marker
                        ("guaranteed", "always", ...) while also failing a
                        groundedness signal
    irrelevant_answer   retrieval failed to find the expected evidence AND
                        the answer has poor token overlap with whatever
                        evidence was retrieved instead
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

from evalharness.schemas import AutomatedScoreRecord, FailureTaxonomyRecord, LLMReviewRecord, Query

TAGS = (
    "unsupported_claim",
    "missed_key_fact",
    "policy_violation",
    "retrieval_miss",
    "overconfident_tone",
    "irrelevant_answer",
)

# Small, fixed lexicon of absolute/overconfidence markers. This is a
# lexical hint combined with a groundedness signal below, not a
# standalone claim that any use of these words is a failure.
_CONFIDENCE_MARKERS = (
    "guaranteed", "guarantee", "always", "never", "definitely", "certainly",
    "100%", "instant", "instantly", "immediately", "no exceptions", "promise",
    "promised", "assured", "without fail",
)
_MARKER_RE = re.compile(
    r"\b(" + "|".join(re.escape(m) for m in _CONFIDENCE_MARKERS) + r")\b", re.IGNORECASE
)


def _has_confidence_marker(text: str) -> bool:
    return bool(_MARKER_RE.search(text))


def tag_answer(
    query: Query,
    score: AutomatedScoreRecord,
    answer_text: str,
    review: Optional[LLMReviewRecord],
    variant: str,
    low_grounding_threshold: float,
    disqualifying_severity: int,
    risk_severity: Dict[str, int],
    unknown_severity: int,
) -> List[str]:
    """Assign zero or more tags to one (query, variant) answer. Order in
    the output follows TAGS, not insertion order, so output is stable.
    """
    present = set()

    judge_overclaim = bool(review and review.overclaim_flags.get(variant))
    if (not score.must_not_claim_pass) or ("unsupported_numeric" in score.risk_flags) or judge_overclaim:
        present.add("unsupported_claim")

    if not score.must_include_pass:
        present.add("missed_key_fact")

    severity = risk_severity.get(query.risk_level, unknown_severity)
    if (not score.must_not_claim_pass) and severity >= disqualifying_severity:
        present.add("policy_violation")

    if not score.retrieval_hit:
        present.add("retrieval_miss")

    if _has_confidence_marker(answer_text) and (
        (not score.must_not_claim_pass) or score.grounding_score < low_grounding_threshold
    ):
        present.add("overconfident_tone")

    if (not score.retrieval_hit) and score.grounding_score < low_grounding_threshold:
        present.add("irrelevant_answer")

    return [t for t in TAGS if t in present]


def build_failure_taxonomy(
    queries: List[Query],
    automated_scores: List[AutomatedScoreRecord],
    llm_reviews: List[LLMReviewRecord],
    answers_by_query: dict,
    config: dict,
) -> List[FailureTaxonomyRecord]:
    """One FailureTaxonomyRecord per (query, variant) already scored by
    the rule-based stage -- iterating automated_scores means this covers
    however many variants are present without hardcoding a count.
    """
    risk_cfg = config["risk"]
    gates_cfg = config["gates"]
    grounding_cfg = config["grounding"]

    reviews_by_qid = {r.query_id: r for r in llm_reviews}
    queries_by_id = {q.query_id: q for q in queries}

    records: List[FailureTaxonomyRecord] = []
    for score in automated_scores:
        query = queries_by_id[score.query_id]
        answer_set = answers_by_query.get(score.query_id)
        answer_text = answer_set.answers.get(score.variant, "") if answer_set else ""
        review = reviews_by_qid.get(score.query_id)

        tags = tag_answer(
            query=query,
            score=score,
            answer_text=answer_text,
            review=review,
            variant=score.variant,
            low_grounding_threshold=grounding_cfg["low_grounding_threshold"],
            disqualifying_severity=gates_cfg["disqualify_must_not_claim_at_severity"],
            risk_severity=risk_cfg["levels"],
            unknown_severity=risk_cfg["unknown_severity"],
        )
        records.append(FailureTaxonomyRecord(query_id=score.query_id, variant=score.variant, tags=tags))
    return records
