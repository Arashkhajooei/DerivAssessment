"""Pydantic models for every file the harness reads and writes.

Design notes
------------
* Models describe the *schema*, never the sample data. No query id, phrase or
  variant name appears anywhere in this module.
* Variant names (``prompt_a``, ``prompt_b``, ...) are dictionary keys, not
  fields, so a fixture may carry any number of variants under any names.
* Unknown extra keys are allowed through rather than rejected, because a
  fixture may legitimately carry extra metadata. The loader reports them as
  warnings so a misspelled field name is still visible instead of silent.
* ``typing.List``/``Dict`` are used rather than PEP 604 unions so the package
  imports cleanly on Python 3.9, which is still the system interpreter on
  many machines.
"""

from __future__ import annotations

from typing import Dict, List

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Shared model configuration: keep unknown keys (the loader warns about them)
# and strip incidental whitespace from strings.
_BASE = ConfigDict(extra="allow", str_strip_whitespace=True)


def _require_non_blank(value: str, field_name: str) -> str:
    if not value or not value.strip():
        raise ValueError("{} must be a non-empty string".format(field_name))
    return value


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------


class KBDocument(BaseModel):
    """One knowledge base passage (``kb.json``)."""

    model_config = _BASE

    doc_id: str
    title: str
    text: str

    @field_validator("doc_id", "text")
    @classmethod
    def _non_blank(cls, v: str, info) -> str:
        return _require_non_blank(v, info.field_name)


class Query(BaseModel):
    """One user question and its answer constraints (``queries.json``).

    The three constraint lists default to empty: a fixture may legitimately
    omit them, and an absent constraint means "nothing to check", not a
    failure. ``risk_level`` is required because it drives the safety gate,
    and a missing value cannot be safely defaulted.
    """

    model_config = _BASE

    query_id: str
    user_question: str
    expected_doc_ids: List[str] = Field(default_factory=list)
    must_include_any: List[str] = Field(default_factory=list)
    must_not_claim: List[str] = Field(default_factory=list)
    risk_level: str

    @field_validator("query_id", "user_question", "risk_level")
    @classmethod
    def _non_blank(cls, v: str, info) -> str:
        return _require_non_blank(v, info.field_name)

    @field_validator("expected_doc_ids", "must_include_any", "must_not_claim")
    @classmethod
    def _drop_blank_entries(cls, v: List[str]) -> List[str]:
        # A blank phrase would match everything; discard rather than let it
        # silently pass every answer.
        return [item for item in v if item and item.strip()]


class AnswerSet(BaseModel):
    """Answers produced by each prompt variant for one query."""

    model_config = _BASE

    query_id: str
    answers: Dict[str, str]

    @field_validator("query_id")
    @classmethod
    def _non_blank(cls, v: str, info) -> str:
        return _require_non_blank(v, info.field_name)

    @field_validator("answers")
    @classmethod
    def _at_least_one_variant(cls, v: Dict[str, str]) -> Dict[str, str]:
        if not v:
            raise ValueError("answers must contain at least one variant")
        for name in v:
            _require_non_blank(name, "variant name")
        return v


# --------------------------------------------------------------------------
# Outputs
# --------------------------------------------------------------------------


class RetrievedPassage(BaseModel):
    """One retrieved evidence passage, as written to ``retrieval.json``."""

    model_config = ConfigDict(extra="forbid")

    doc_id: str
    score: float
    title: str
    text: str


class RetrievalRecord(BaseModel):
    """``retrieval.json`` record: the evidence package for one query."""

    model_config = ConfigDict(extra="forbid")

    query_id: str
    retrieved: List[RetrievedPassage]


class AutomatedScoreRecord(BaseModel):
    """``automated_scores.json`` record: deterministic checks for one answer."""

    model_config = ConfigDict(extra="forbid")

    query_id: str
    variant: str
    retrieval_hit: bool
    must_include_pass: bool
    must_not_claim_pass: bool
    grounding_score: float
    risk_flags: List[str]
    notes: str


class LLMReviewRecord(BaseModel):
    """``llm_review.json`` record: the judge's verdict for one query.

    The per-variant maps are keyed by variant name rather than by fixed
    fields, so a third variant needs no schema change.
    """

    model_config = ConfigDict(extra="forbid")

    query_id: str
    winner: str
    faithfulness: Dict[str, int]
    clarity: Dict[str, int]
    overclaim_flags: Dict[str, bool]
    justification: str


class FailureTaxonomyRecord(BaseModel):
    """``failure_taxonomy.json`` record: failure tags for one answer."""

    model_config = ConfigDict(extra="forbid")

    query_id: str
    variant: str
    tags: List[str]
