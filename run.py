#!/usr/bin/env python3
"""Pipeline entrypoint. Regenerates every derived artifact from the input
files and config.yaml: retrieval, rule-based scoring, the LLM review
stage, the failure taxonomy, the aggregated recommendation, the
explainability view, and a run manifest recording exactly what produced
them.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Dict, List

from evalharness.aggregate import compute_recommendation, render_recommendation_markdown
from evalharness.checks import evaluate_answer
from evalharness.config import load_config
from evalharness.errors import InputValidationError
from evalharness.judge import run_judge_stage
from evalharness.loader import LoadedInputs, load_inputs
from evalharness.manifest import build_run_manifest
from evalharness.retrieval import BM25Index, ScoredPassage
from evalharness.review_report import render_review_report
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
    Returns query_id -> retrieved passages, reused by every later stage
    so retrieval only runs once per query.
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
):
    recommendation = compute_recommendation(
        inputs.queries, automated_scores, llm_reviews, failure_taxonomy, config
    )
    markdown = render_recommendation_markdown(
        recommendation, inputs.queries, automated_scores, llm_reviews, judge_backend, config
    )
    with open(config["paths"]["recommendation"], "w", encoding="utf-8") as fh:
        fh.write(markdown)

    print(
        "Selected variant: {}".format(
            recommendation.selected_variant or "NONE (no promotion recommended)"
        )
    )
    for reason in recommendation.reasons:
        print("  reason:", reason)
    for tradeoff in recommendation.tradeoffs:
        print("  tradeoff:", tradeoff)

    return recommendation


def run_review_report_stage(
    inputs: LoadedInputs,
    retrieved_by_query: Dict[str, List[ScoredPassage]],
    automated_scores: List[AutomatedScoreRecord],
    llm_reviews: List[LLMReviewRecord],
    failure_taxonomy: List[FailureTaxonomyRecord],
    selected_variant,
    config: dict,
) -> None:
    markdown = render_review_report(
        inputs.queries,
        retrieved_by_query,
        inputs.answers_by_query,
        automated_scores,
        llm_reviews,
        failure_taxonomy,
        selected_variant,
    )
    with open(config["paths"]["review_report"], "w", encoding="utf-8") as fh:
        fh.write(markdown)


def main() -> int:
    config = load_config("config.yaml")
    paths = config["paths"]
    timings: Dict[str, float] = {}

    def timed(name, fn, *args):
        start = time.monotonic()
        result = fn(*args)
        timings[name] = time.monotonic() - start
        return result

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

    retrieved_by_query = timed("retrieval", run_retrieval_stage, inputs, config)
    print("Wrote {} ({} queries)".format(paths["retrieval"], len(retrieved_by_query)))

    scores = timed("automated_scores", run_automated_scores_stage, inputs, retrieved_by_query, config)
    print("Wrote {} ({} query x variant records)".format(paths["automated_scores"], len(scores)))

    llm_reviews, judge_backend = timed(
        "llm_review", run_llm_review_stage, inputs, retrieved_by_query, scores, config
    )
    print(
        "Wrote {} ({} records, judge backend: {})".format(
            paths["llm_review"], len(llm_reviews), judge_backend
        )
    )

    taxonomy = timed("failure_taxonomy", run_failure_taxonomy_stage, inputs, scores, llm_reviews, config)
    print("Wrote {} ({} records)".format(paths["failure_taxonomy"], len(taxonomy)))

    recommendation = timed(
        "recommendation", run_recommendation_stage, inputs, scores, llm_reviews, taxonomy, judge_backend, config
    )
    print("Wrote {}".format(paths["recommendation"]))

    timed(
        "review_report",
        run_review_report_stage,
        inputs,
        retrieved_by_query,
        scores,
        llm_reviews,
        taxonomy,
        recommendation.selected_variant,
        config,
    )
    print("Wrote {}".format(paths["review_report"]))

    manifest = build_run_manifest(
        config=config,
        input_paths={
            "kb.json": paths["kb"],
            "queries.json": paths["queries"],
            "candidate_answers.json": paths["candidate_answers"],
        },
        counts={
            "kb_docs": len(inputs.kb),
            "queries": len(inputs.queries),
            "variants": len(inputs.variant_names),
        },
        judge_backend=judge_backend,
        stage_timings_seconds=timings,
    )
    with open(paths["run_manifest"], "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
        fh.write("\n")
    print("Wrote {}".format(paths["run_manifest"]))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
