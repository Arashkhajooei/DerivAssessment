#!/usr/bin/env python3
"""Pipeline entrypoint. Regenerates every derived artifact from the input
files and config.yaml.

Implemented: retrieval, rule-based scoring, the LLM review stage (cache ->
live -> stub), the failure taxonomy, and the aggregated recommendation.
Not yet implemented: the explainability view (review_report.md, stretch
goal) and the run manifest / provenance metadata.
"""

from __future__ import annotations

import json
import sys
from typing import Dict, List

from evalharness.aggregate import compute_recommendation, render_recommendation_markdown
from evalharness.checks import evaluate_answer
from evalharness.config import load_config
from evalharness.errors import InputValidationError
from evalharness.judge import run_judge_stage
from evalharness.loader import LoadedInputs, load_inputs
from evalharness.retrieval import BM25Index, ScoredPassage
from evalharness.schemas import (
    AutomatedScoreRecord,
    FailureTaxonomyRecord,
    LLMReviewRecord,
    RetrievalRecord,
    RetrievedPassage,
)
from evalharness.taxonomy import build_failure_taxonomy


def run_retrieval_stage(inputs: LoadedInputs, config: dict) -> Dict[str, List[ScoredPassage]]:
    """Build the BM25 index once and retrieve top_k passages per query.
    Returns query_id -> retrieved passages, reused by the scoring and
    judge stages so retrieval only runs once per query.
    """
    retrieval_cfg = config["retrieval"]
    index = BM25Index(inputs.kb, k1=retrieval_cfg["bm25_k1"], b=retrieval_cfg["bm25_b"])

    retrieved_by_query: Dict[str, List[ScoredPassage]] = {}
    records: List[RetrievalRecord] = []
    for query in inputs.queries:
        passages = index.search(
            query.user_question,
            top_k=retrieval_cfg["top_k"],
            score_precision=retrieval_cfg["score_precision"],
        )
        retrieved_by_query[query.query_id] = passages
        records.append(
            RetrievalRecord(
                query_id=query.query_id,
                retrieved=[
                    RetrievedPassage(doc_id=p.doc_id, score=p.score, title=p.title, text=p.text)
                    for p in passages
                ],
            )
        )

    with open(config["paths"]["retrieval"], "w", encoding="utf-8") as fh:
        json.dump([r.model_dump() for r in records], fh, indent=2)
        fh.write("\n")

    return retrieved_by_query


def run_automated_scores_stage(
    inputs: LoadedInputs, retrieved_by_query: Dict[str, List[ScoredPassage]], config: dict
) -> List[AutomatedScoreRecord]:
    """Run every rule-based check for every (query, variant) pair. Variant
    order comes from `inputs.variant_names` (discovered from
    candidate_answers.json), so this loop is unchanged for 1, 2, or N
    variants.
    """
    records: List[AutomatedScoreRecord] = []
    for query in inputs.queries:
        retrieved = retrieved_by_query.get(query.query_id, [])
        answer_set = inputs.answers_by_query.get(query.query_id)
        if answer_set is None:
            continue  # already surfaced as a MISSING_ANSWERS warning by the loader
        for variant in inputs.variant_names:
            answer_text = answer_set.answers.get(variant)
            if answer_text is None:
                continue  # inconsistent variant coverage; already warned by the loader
            records.append(evaluate_answer(query, variant, answer_text, retrieved, config))

    with open(config["paths"]["automated_scores"], "w", encoding="utf-8") as fh:
        json.dump([r.model_dump() for r in records], fh, indent=2)
        fh.write("\n")

    return records


def run_llm_review_stage(
    inputs: LoadedInputs,
    retrieved_by_query: Dict[str, List[ScoredPassage]],
    automated_scores: List[AutomatedScoreRecord],
    config: dict,
) -> "tuple[List[LLMReviewRecord], str]":
    """One controlled, batched LLM call (or cache replay, or deterministic
    stub -- see evalharness.judge) covering every query/answer pair.
    """
    records, backend = run_judge_stage(inputs, retrieved_by_query, automated_scores, config)

    with open(config["paths"]["llm_review"], "w", encoding="utf-8") as fh:
        json.dump([r.model_dump() for r in records], fh, indent=2)
        fh.write("\n")

    return records, backend


def run_failure_taxonomy_stage(
    inputs: LoadedInputs,
    automated_scores: List[AutomatedScoreRecord],
    llm_reviews: List[LLMReviewRecord],
    config: dict,
) -> List[FailureTaxonomyRecord]:
    records = build_failure_taxonomy(
        inputs.queries, automated_scores, llm_reviews, inputs.answers_by_query, config
    )

    with open(config["paths"]["failure_taxonomy"], "w", encoding="utf-8") as fh:
        json.dump([r.model_dump() for r in records], fh, indent=2)
        fh.write("\n")

    return records


def run_recommendation_stage(
    inputs: LoadedInputs,
    automated_scores: List[AutomatedScoreRecord],
    llm_reviews: List[LLMReviewRecord],
    failure_taxonomy: List[FailureTaxonomyRecord],
    judge_backend: str,
    config: dict,
) -> None:
    recommendation = compute_recommendation(
        inputs.queries, automated_scores, llm_reviews, failure_taxonomy, config
    )
    markdown = render_recommendation_markdown(
        recommendation, inputs.queries, automated_scores, llm_reviews, judge_backend, config
    )
    with open(config["paths"]["recommendation"], "w", encoding="utf-8") as fh:
        fh.write(markdown)

    print(
        "Selected variant: {}".format(recommendation.selected_variant or "NONE (no promotion recommended)")
    )
    for reason in recommendation.reasons:
        print("  reason:", reason)
    for tradeoff in recommendation.tradeoffs:
        print("  tradeoff:", tradeoff)


def main() -> int:
    config = load_config("config.yaml")
    paths = config["paths"]

    try:
        inputs = load_inputs(paths["kb"], paths["queries"], paths["candidate_answers"])
    except InputValidationError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(
        "Loaded {} KB docs, {} queries, {} variants: {}".format(
            len(inputs.kb), len(inputs.queries), len(inputs.variant_names), inputs.variant_names
        )
    )

    retrieved_by_query = run_retrieval_stage(inputs, config)
    print("Wrote {} ({} queries)".format(paths["retrieval"], len(retrieved_by_query)))

    scores = run_automated_scores_stage(inputs, retrieved_by_query, config)
    print("Wrote {} ({} query x variant records)".format(paths["automated_scores"], len(scores)))

    llm_reviews, judge_backend = run_llm_review_stage(inputs, retrieved_by_query, scores, config)
    print(
        "Wrote {} ({} records, judge backend: {})".format(
            paths["llm_review"], len(llm_reviews), judge_backend
        )
    )

    taxonomy = run_failure_taxonomy_stage(inputs, scores, llm_reviews, config)
    print("Wrote {} ({} records)".format(paths["failure_taxonomy"], len(taxonomy)))

    run_recommendation_stage(inputs, scores, llm_reviews, taxonomy, judge_backend, config)
    print("Wrote {}".format(paths["recommendation"]))

    print()
    print("NOT YET IMPLEMENTED (planned, see README):")
    print("  - Explainability view    -> review_report.md (stretch goal)")
    print("  - Run manifest / provenance metadata")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
