"""Tests for the failure taxonomy: each tag's derivation rule, checked
against synthetic scores rather than the real fixtures, so the assertions
describe the *rule*, not "whatever the sample data happens to produce."
"""

from evalharness.schemas import AutomatedScoreRecord, Query
from evalharness.taxonomy import TAGS, tag_answer

_RISK_SEVERITY = {"low": 1, "medium": 2, "high": 3, "critical": 4}


def _query(risk_level="low"):
    return Query(query_id="Q1", user_question="hi?", risk_level=risk_level)


def _score(**overrides):
    base = dict(
        query_id="Q1",
        variant="v1",
        retrieval_hit=True,
        must_include_pass=True,
        must_not_claim_pass=True,
        grounding_score=0.9,
        risk_flags=["none"],
        notes="",
    )
    base.update(overrides)
    return AutomatedScoreRecord(**base)


def _tag(query, score, answer_text="An ordinary answer.", review=None, low_grounding=0.3, severity=3):
    return tag_answer(
        query=query,
        score=score,
        answer_text=answer_text,
        review=review,
        variant="v1",
        low_grounding_threshold=low_grounding,
        disqualifying_severity=severity,
        risk_severity=_RISK_SEVERITY,
        unknown_severity=4,
    )


class TestEachTagRule:
    def test_clean_answer_gets_no_tags(self):
        assert _tag(_query(), _score()) == []

    def test_must_not_claim_failure_tags_unsupported_claim(self):
        tags = _tag(_query(), _score(must_not_claim_pass=False))
        assert "unsupported_claim" in tags

    def test_unsupported_numeric_tags_unsupported_claim(self):
        tags = _tag(_query(), _score(risk_flags=["unsupported_numeric"]))
        assert "unsupported_claim" in tags

    def test_must_include_failure_tags_missed_key_fact(self):
        tags = _tag(_query(), _score(must_include_pass=False))
        assert tags == ["missed_key_fact"]

    def test_policy_violation_only_fires_above_severity_threshold(self):
        low_risk_tags = _tag(_query(risk_level="low"), _score(must_not_claim_pass=False), severity=3)
        assert "policy_violation" not in low_risk_tags

        high_risk_tags = _tag(_query(risk_level="high"), _score(must_not_claim_pass=False), severity=3)
        assert "policy_violation" in high_risk_tags

    def test_retrieval_miss(self):
        tags = _tag(_query(), _score(retrieval_hit=False, grounding_score=0.9))
        assert "retrieval_miss" in tags
        # grounding is high here, so irrelevant_answer should NOT also fire
        assert "irrelevant_answer" not in tags

    def test_irrelevant_answer_requires_both_retrieval_miss_and_low_grounding(self):
        tags = _tag(_query(), _score(retrieval_hit=False, grounding_score=0.1), low_grounding=0.3)
        assert "irrelevant_answer" in tags

    def test_overconfident_tone_requires_marker_and_a_groundedness_failure(self):
        # Marker present but answer is otherwise clean -> should NOT fire.
        clean = _tag(_query(), _score(grounding_score=0.9), answer_text="This is guaranteed to work.")
        assert "overconfident_tone" not in clean

        # Marker present AND must_not_claim failed -> should fire.
        dirty = _tag(
            _query(), _score(must_not_claim_pass=False), answer_text="This is guaranteed to work."
        )
        assert "overconfident_tone" in dirty

    def test_tag_order_follows_fixed_vocabulary_order(self):
        score = _score(must_not_claim_pass=False, must_include_pass=False, retrieval_hit=False)
        tags = _tag(_query(risk_level="high"), score, answer_text="It is guaranteed.", severity=3)
        assert tags == [t for t in TAGS if t in tags]
