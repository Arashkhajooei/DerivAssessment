"""Explainability view: a compact, per-query human-readable rendering of
everything the pipeline decided -- question, retrieved evidence, every
variant's answer, which rule checks failed, the judge's per-question
winner, and the overall selected variant -- so debugging a single query
never requires cross-referencing four separate JSON files by hand.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from evalharness.retrieval import ScoredPassage
from evalharness.schemas import AutomatedScoreRecord, FailureTaxonomyRecord, LLMReviewRecord, Query


def render_review_report(
    queries: List[Query],
    retrieved_by_query: Dict[str, List[ScoredPassage]],
    answers_by_query: dict,
    automated_scores: List[AutomatedScoreRecord],
    llm_reviews: List[LLMReviewRecord],
    failure_taxonomy: List[FailureTaxonomyRecord],
    selected_variant: Optional[str],
) -> str:
    scores_by_key = {(s.query_id, s.variant): s for s in automated_scores}
    taxonomy_by_key = {(t.query_id, t.variant): t for t in failure_taxonomy}
    reviews_by_qid = {r.query_id: r for r in llm_reviews}

    lines: List[str] = ["# Explainability Review", ""]
    lines.append(
        "Overall selected variant for promotion: **{}**".format(
            selected_variant or "none (no promotion recommended)"
        )
    )
    lines.append("")

    for query in queries:
        answer_set = answers_by_query.get(query.query_id)
        if answer_set is None:
            continue
        variants = sorted(answer_set.answers)
        review = reviews_by_qid.get(query.query_id)

        lines.append("## {} ({} risk)".format(query.query_id, query.risk_level))
        lines.append("")
        lines.append("**Question:** {}".format(query.user_question))
        lines.append("")

        lines.append("**Retrieved evidence:**")
        retrieved = retrieved_by_query.get(query.query_id, [])
        if retrieved:
            for p in retrieved:
                lines.append("- `{}` (score {}) {}: {}".format(p.doc_id, p.score, p.title, p.text))
        else:
            lines.append("- (none retrieved)")
        lines.append("")

        for v in variants:
            score = scores_by_key.get((query.query_id, v))
            taxonomy = taxonomy_by_key.get((query.query_id, v))
            lines.append('**{}:** "{}"'.format(v, answer_set.answers[v]))
            if score is not None:
                failed = []
                if not score.retrieval_hit:
                    failed.append("retrieval_hit")
                if not score.must_include_pass:
                    failed.append("must_include_pass")
                if not score.must_not_claim_pass:
                    failed.append("must_not_claim_pass")
                lines.append(
                    "  - grounding_score={}, failed checks: {}".format(
                        score.grounding_score, ", ".join(failed) if failed else "none"
                    )
                )
            if taxonomy and taxonomy.tags:
                lines.append("  - failure tags: {}".format(", ".join(taxonomy.tags)))
            lines.append("")

        lines.append("**Judge winner for this question:** {}".format(review.winner if review else "n/a"))
        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines)
