#!/usr/bin/env python3
"""Validates a completed run's artifacts: presence, well-formedness,
internal consistency, and that the final recommendation is reproducible
from the stored artifacts alone. These are the six checks the brief's
"Validation Command" section requires.

Exit code 0 means every check passed. Exit code 1 means at least one
check failed; every failure found is printed, not just the first one.
"""

from __future__ import annotations

import json
import sys
from typing import Dict, List

from pydantic import ValidationError

from evalharness.aggregate import compute_recommendation, render_recommendation_markdown
from evalharness.config import load_config
from evalharness.errors import ERROR, Issue
from evalharness.judge import validate_reviews
from evalharness.loader import load_inputs
from evalharness.schemas import AutomatedScoreRecord, FailureTaxonomyRecord, LLMReviewRecord, RetrievalRecord


def _read_json_array(path: str, issues: List[Issue]) -> list:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        issues.append(Issue("MISSING_ARTIFACT", "file does not exist", path))
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        issues.append(Issue("INVALID_JSON", str(exc), path))
        return []
    if not isinstance(data, list):
        issues.append(Issue("NOT_AN_ARRAY", "expected a top-level JSON array", path))
        return []
    return data


def _read_jsonl(path: str, issues: List[Issue]) -> list:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except FileNotFoundError:
        issues.append(Issue("MISSING_ARTIFACT", "file does not exist", path))
        return []
    entries = []
    for i, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError as exc:
            issues.append(Issue("INVALID_JSON", str(exc), "{}:{}".format(path, i + 1)))
    return entries


def _parse_records(data: list, model, path: str, issues: List[Issue]) -> list:
    records = []
    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            issues.append(Issue("NOT_AN_OBJECT", "record is not a JSON object", "{}[{}]".format(path, idx)))
            continue
        try:
            records.append(model(**item))
        except ValidationError as exc:
            for err in exc.errors():
                field = ".".join(str(p) for p in err["loc"]) or "<root>"
                issues.append(Issue("SCHEMA_INVALID", err["msg"], "{}[{}].{}".format(path, idx, field)))
    return records


def check_artifacts_exist_and_parse(paths: dict, issues: List[Issue]) -> Dict[str, list]:
    """Checks #1 and #2: required artifacts exist, and every JSON/JSONL
    and markdown file is present and syntactically/structurally valid.
    """
    required = [
        (paths["retrieval"], RetrievalRecord),
        (paths["automated_scores"], AutomatedScoreRecord),
        (paths["llm_review"], LLMReviewRecord),
        (paths["failure_taxonomy"], FailureTaxonomyRecord),
    ]
    parsed: Dict[str, list] = {}
    for path, model in required:
        data = _read_json_array(path, issues)
        parsed[path] = _parse_records(data, model, path, issues) if data else []

    for markdown_path in (paths["recommendation"], paths["review_report"]):
        try:
            with open(markdown_path, "r", encoding="utf-8") as fh:
                if not fh.read().strip():
                    issues.append(Issue("EMPTY_ARTIFACT", "file exists but is empty", markdown_path))
        except FileNotFoundError:
            issues.append(Issue("MISSING_ARTIFACT", "file does not exist", markdown_path))

    _read_jsonl(paths["llm_calls"], issues)

    try:
        with open(paths["run_manifest"], "r", encoding="utf-8") as fh:
            json.loads(fh.read())
    except FileNotFoundError:
        issues.append(Issue("MISSING_ARTIFACT", "file does not exist", paths["run_manifest"]))
    except json.JSONDecodeError as exc:
        issues.append(Issue("INVALID_JSON", str(exc), paths["run_manifest"]))

    return parsed


def check_all_queries_processed(inputs, automated_scores: List[AutomatedScoreRecord], issues: List[Issue]) -> None:
    """Check #3: every (query_id, variant) pair present in
    candidate_answers.json has a corresponding automated_scores.json
    record.
    """
    expected = {
        (qid, variant)
        for qid, answer_set in inputs.answers_by_query.items()
        for variant in answer_set.answers
    }
    actual = {(r.query_id, r.variant) for r in automated_scores}
    for qid, variant in sorted(expected - actual):
        issues.append(
            Issue(
                "UNPROCESSED_ANSWER",
                "no automated_scores.json record for query_id='{}' variant='{}'".format(qid, variant),
                "automated_scores.json",
            )
        )


def check_retrieval_top_k(
    inputs, retrieval_records: List[RetrievalRecord], config: dict, issues: List[Issue]
) -> None:
    """Check #4: retrieval output contains the configured top_k passages
    per query, or fewer only when the knowledge base itself has fewer
    documents than top_k (a small corpus, not an error).
    """
    top_k = config["retrieval"]["top_k"]
    expected_count = min(top_k, len(inputs.kb))
    by_qid = {r.query_id: r for r in retrieval_records}
    for query in inputs.queries:
        record = by_qid.get(query.query_id)
        if record is None:
            issues.append(
                Issue(
                    "MISSING_RETRIEVAL",
                    "no retrieval.json record for this query",
                    "query_id={}".format(query.query_id),
                )
            )
            continue
        if len(record.retrieved) != expected_count:
            issues.append(
                Issue(
                    "WRONG_RETRIEVAL_COUNT",
                    "expected {} passages (top_k={}, kb_size={}), got {}".format(
                        expected_count, top_k, len(inputs.kb), len(record.retrieved)
                    ),
                    "retrieval.json[query_id={}]".format(query.query_id),
                )
            )


def check_llm_review_values(
    inputs, llm_reviews: List[LLMReviewRecord], config: dict, issues: List[Issue]
) -> None:
    """Check #5: LLM review values use only allowed variants and expected
    score ranges. Reuses the exact validation the judge stage applies to
    a live response, so there is one source of truth for "a valid review."
    """
    payload = {"reviews": [r.model_dump() for r in llm_reviews]}
    _, errors = validate_reviews(payload, inputs, config["judge"]["score_min"], config["judge"]["score_max"])
    for err in errors:
        issues.append(Issue("INVALID_LLM_REVIEW", err, "llm_review.json"))


def check_recommendation_reproducible(
    inputs,
    automated_scores: List[AutomatedScoreRecord],
    llm_reviews: List[LLMReviewRecord],
    failure_taxonomy: List[FailureTaxonomyRecord],
    config: dict,
    issues: List[Issue],
) -> None:
    """Check #6: the recommendation is reproducible from stored outputs
    alone. Recomputes it from the already-parsed artifacts plus
    run_manifest.json's recorded judge backend, and diffs the result
    byte-for-byte against recommendation.md on disk.
    """
    try:
        with open(config["paths"]["run_manifest"], "r", encoding="utf-8") as fh:
            manifest = json.loads(fh.read())
        judge_backend = manifest["judge"]["backend_used"]
    except (FileNotFoundError, json.JSONDecodeError, KeyError) as exc:
        issues.append(
            Issue(
                "MANIFEST_UNREADABLE",
                "could not read judge.backend_used from run_manifest.json: {}".format(exc),
                config["paths"]["run_manifest"],
            )
        )
        return

    recomputed = compute_recommendation(inputs.queries, automated_scores, llm_reviews, failure_taxonomy, config)
    recomputed_markdown = render_recommendation_markdown(
        recomputed, inputs.queries, automated_scores, llm_reviews, judge_backend, config
    )

    try:
        with open(config["paths"]["recommendation"], "r", encoding="utf-8") as fh:
            on_disk = fh.read()
    except FileNotFoundError:
        issues.append(Issue("MISSING_ARTIFACT", "file does not exist", config["paths"]["recommendation"]))
        return

    if recomputed_markdown != on_disk:
        issues.append(
            Issue(
                "RECOMMENDATION_NOT_REPRODUCIBLE",
                "recomputing the recommendation from stored artifacts does not match "
                "recommendation.md on disk -- it may be stale (regenerate with `python run.py`)",
                config["paths"]["recommendation"],
            )
        )


def main() -> int:
    config = load_config("config.yaml")
    paths = config["paths"]
    issues: List[Issue] = []

    try:
        inputs = load_inputs(paths["kb"], paths["queries"], paths["candidate_answers"])
    except Exception as exc:  # noqa: BLE001 -- input files themselves must be valid to validate anything else
        print("FAILED: could not load input files: {}".format(exc), file=sys.stderr)
        return 1

    parsed = check_artifacts_exist_and_parse(paths, issues)
    automated_scores = parsed.get(paths["automated_scores"], [])
    retrieval_records = parsed.get(paths["retrieval"], [])
    llm_reviews = parsed.get(paths["llm_review"], [])
    failure_taxonomy = parsed.get(paths["failure_taxonomy"], [])

    if automated_scores:
        check_all_queries_processed(inputs, automated_scores, issues)
    if retrieval_records:
        check_retrieval_top_k(inputs, retrieval_records, config, issues)
    if llm_reviews:
        check_llm_review_values(inputs, llm_reviews, config, issues)
    if automated_scores and llm_reviews and failure_taxonomy:
        check_recommendation_reproducible(inputs, automated_scores, llm_reviews, failure_taxonomy, config, issues)

    errors = [i for i in issues if i.severity == ERROR]
    if errors:
        print("VALIDATION FAILED with {} issue(s):".format(len(errors)), file=sys.stderr)
        for n, issue in enumerate(errors, 1):
            print("  {}. {}".format(n, issue.render()), file=sys.stderr)
        return 1

    print("VALIDATION PASSED:")
    print("  - all required artifacts exist and are well-formed JSON/JSONL")
    print("  - every query x variant pair in candidate_answers.json was processed")
    print("  - retrieval.json contains the configured top_k passages per query")
    print("  - llm_review.json values use only valid variants and score ranges")
    print("  - recommendation.md matches a fresh recompute from stored artifacts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
