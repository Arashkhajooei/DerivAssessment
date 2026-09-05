"""Direct unit tests on the Pydantic models, independent of file loading."""

import pytest
from pydantic import ValidationError

from evalharness.schemas import AnswerSet, KBDocument, Query


class TestQueryDefaults:
    def test_constraint_lists_default_to_empty(self):
        q = Query(query_id="Q1", user_question="hi?", risk_level="low")
        assert q.expected_doc_ids == []
        assert q.must_include_any == []
        assert q.must_not_claim == []

    def test_blank_entries_in_constraint_lists_are_dropped(self):
        q = Query(
            query_id="Q1",
            user_question="hi?",
            risk_level="low",
            must_include_any=["real phrase", "  ", ""],
        )
        assert q.must_include_any == ["real phrase"]

    def test_missing_risk_level_raises(self):
        with pytest.raises(ValidationError):
            Query(query_id="Q1", user_question="hi?")


class TestAnswerSetVariants:
    def test_arbitrary_variant_count_and_names(self):
        a = AnswerSet(query_id="Q1", answers={"x": "1", "y": "2", "z": "3", "w": "4"})
        assert set(a.answers) == {"x", "y", "z", "w"}

    def test_empty_answers_map_raises(self):
        with pytest.raises(ValidationError):
            AnswerSet(query_id="Q1", answers={})


class TestKBDocument:
    def test_blank_doc_id_raises(self):
        with pytest.raises(ValidationError):
            KBDocument(doc_id="   ", title="t", text="some text")

    def test_blank_text_raises(self):
        with pytest.raises(ValidationError):
            KBDocument(doc_id="D1", title="t", text="")
