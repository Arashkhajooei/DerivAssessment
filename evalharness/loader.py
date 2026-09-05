"""Loading and validating the three input files.

This module is where "may replace the input files with equivalent fixtures"
is actually enforced. Every check here is schema-level: it validates shape
and cross-references, never specific ids, phrases or answer text. A swapped
fixture with different ids, a third query, three variants, or no
constraints at all must pass through unchanged.

Validation runs in two passes:
  1. Structural -- does each file parse as JSON and match its Pydantic model?
  2. Referential -- do cross-file references actually resolve?

All issues from both passes are collected and reported together, because a
single run should tell the evaluator everything wrong with a fixture, not
one error per rerun.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Dict, List, Tuple

from pydantic import ValidationError

from evalharness.errors import ERROR, WARNING, InputValidationError, Issue
from evalharness.schemas import AnswerSet, KBDocument, Query


@dataclass(frozen=True)
class LoadedInputs:
    """Validated, cross-referenced input data, ready for the pipeline."""

    kb: List[KBDocument]
    queries: List[Query]
    answers: List[AnswerSet]

    kb_by_id: Dict[str, KBDocument]
    queries_by_id: Dict[str, Query]
    answers_by_query: Dict[str, AnswerSet]
    variant_names: Tuple[str, ...]  # stable order: first-seen across answers.json


def _read_json_array(path: str, issues: List[Issue]) -> List[dict]:
    """Read a file expected to contain a top-level JSON array.

    Returns [] on any failure after recording an issue, so callers can keep
    validating the other files instead of raising immediately.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw_text = fh.read()
    except FileNotFoundError:
        issues.append(Issue("FILE_NOT_FOUND", "file does not exist", path))
        return []
    except OSError as exc:
        issues.append(Issue("FILE_UNREADABLE", str(exc), path))
        return []

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        issues.append(Issue("INVALID_JSON", "{}".format(exc), path))
        return []

    if not isinstance(data, list):
        issues.append(
            Issue("NOT_AN_ARRAY", "expected a top-level JSON array", path)
        )
        return []

    return data


def _validate_records(data: List[dict], model, path: str, id_field: str, issues: List[Issue]) -> list:
    """Validate each raw record against a Pydantic model.

    A single bad record does not abort the file: the record is skipped and
    every other record is still validated, so one typo doesn't hide the
    rest of the problems in a large fixture.
    """
    validated = []
    seen_ids: Dict[str, int] = {}
    for idx, record in enumerate(data):
        location = "{}[{}]".format(path, idx)
        if not isinstance(record, dict):
            issues.append(Issue("NOT_AN_OBJECT", "record is not a JSON object", location))
            continue
        try:
            obj = model(**record)
        except ValidationError as exc:
            for err in exc.errors():
                field = ".".join(str(p) for p in err["loc"]) or "<root>"
                issues.append(
                    Issue("SCHEMA_INVALID", err["msg"], "{}.{}".format(location, field))
                )
            continue

        rec_id = getattr(obj, id_field)
        if rec_id in seen_ids:
            issues.append(
                Issue(
                    "DUPLICATE_ID",
                    "duplicate {} '{}' (first seen at index {})".format(
                        id_field, rec_id, seen_ids[rec_id]
                    ),
                    "{}.{}".format(location, id_field),
                )
            )
            continue
        seen_ids[rec_id] = idx
        validated.append(obj)

    return validated


def load_inputs(
    kb_path: str,
    queries_path: str,
    answers_path: str,
    strict: bool = True,
) -> LoadedInputs:
    """Load and validate the three input files.

    Raises ``InputValidationError`` (carrying every issue found) if any
    error-severity issue is present. Warnings do not block the pipeline.

    ``strict=False`` is used by tests that want to inspect partial results
    without a raise; the CLI always calls this with the default.
    """
    issues: List[Issue] = []

    kb_raw = _read_json_array(kb_path, issues)
    queries_raw = _read_json_array(queries_path, issues)
    answers_raw = _read_json_array(answers_path, issues)

    kb = _validate_records(kb_raw, KBDocument, kb_path, "doc_id", issues)
    queries = _validate_records(queries_raw, Query, queries_path, "query_id", issues)
    answers = _validate_records(answers_raw, AnswerSet, answers_path, "query_id", issues)

    kb_by_id = {d.doc_id: d for d in kb}
    queries_by_id = {q.query_id: q for q in queries}
    answers_by_query = {a.query_id: a for a in answers}

    if not kb:
        issues.append(Issue("EMPTY_KB", "knowledge base contains no valid documents", kb_path))
    if not queries:
        issues.append(Issue("EMPTY_QUERIES", "no valid queries found", queries_path))
    if not answers:
        issues.append(Issue("EMPTY_ANSWERS", "no valid answer records found", answers_path))

    # --- Referential integrity -------------------------------------------------

    # expected_doc_ids must resolve into the KB.
    for q in queries:
        for doc_id in q.expected_doc_ids:
            if doc_id not in kb_by_id:
                issues.append(
                    Issue(
                        "DANGLING_DOC_ID",
                        "expected_doc_ids references unknown doc_id '{}'".format(doc_id),
                        "{}[query_id={}].expected_doc_ids".format(queries_path, q.query_id),
                    )
                )

    # Every answer record must reference a real query.
    for a in answers:
        if a.query_id not in queries_by_id:
            issues.append(
                Issue(
                    "DANGLING_QUERY_ID",
                    "answers.json references unknown query_id '{}'".format(a.query_id),
                    "{}[query_id={}]".format(answers_path, a.query_id),
                )
            )

    # Every query should have a matching answer record. Missing coverage is a
    # warning, not a hard error: a partial fixture (e.g. during dev) should
    # still let the pipeline run over what it has, and downstream stages
    # decide how to handle a query with no answers.
    for q in queries:
        if q.query_id not in answers_by_query:
            issues.append(
                Issue(
                    "MISSING_ANSWERS",
                    "no answer record found for query_id '{}'".format(q.query_id),
                    "{}[query_id={}]".format(answers_path, q.query_id),
                    severity=WARNING,
                )
            )

    # Variant coverage must be consistent across queries: every query's
    # answer set should carry the same variant names, in the same set, or
    # the aggregation stage would be comparing unequal numbers of strategies
    # across questions.
    variant_order: List[str] = []
    variant_seen = set()
    for a in answers:
        for name in a.answers:
            if name not in variant_seen:
                variant_seen.add(name)
                variant_order.append(name)

    for a in answers:
        missing_variants = variant_seen - set(a.answers)
        for missing in sorted(missing_variants):
            issues.append(
                Issue(
                    "INCONSISTENT_VARIANTS",
                    "query_id '{}' is missing variant '{}' present elsewhere".format(
                        a.query_id, missing
                    ),
                    "{}[query_id={}].answers".format(answers_path, a.query_id),
                    severity=WARNING,
                )
            )

    error_issues = [i for i in issues if i.severity == ERROR]
    if strict and error_issues:
        raise InputValidationError(issues)

    return LoadedInputs(
        kb=kb,
        queries=queries,
        answers=answers,
        kb_by_id=kb_by_id,
        queries_by_id=queries_by_id,
        answers_by_query=answers_by_query,
        variant_names=tuple(variant_order),
    )
