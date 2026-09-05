"""Tests for evalharness.loader: schema validation and referential integrity.

These are the tests that stand in for "the evaluator swaps the input files
for an equivalent fixture" -- every broken fixture here has a valid-looking
JSON shape but violates one specific rule, and every check asserts on error
*codes*, never on hardcoded ids or text, matching the same discipline the
harness itself is held to.
"""

import os

import pytest

from evalharness.errors import InputValidationError
from evalharness.loader import load_inputs


def _codes(exc: InputValidationError):
    return {issue.code for issue in exc.issues}


class TestRealInputs:
    def test_loads_without_raising(self, real_inputs):
        kb, q, a = real_inputs
        result = load_inputs(kb, q, a)
        assert len(result.kb) > 0
        assert len(result.queries) > 0
        assert len(result.answers) > 0

    def test_discovers_variants_from_data(self, real_inputs):
        # Must not hardcode "prompt_a"/"prompt_b" anywhere in the harness;
        # this only asserts that *some* variants were discovered and that
        # every query's answer set carries exactly the same set.
        kb, q, a = real_inputs
        result = load_inputs(kb, q, a)
        assert len(result.variant_names) >= 1
        for record in result.answers:
            assert set(record.answers) == set(result.variant_names)

    def test_every_query_has_a_kb_backed_expectation(self, real_inputs):
        kb, q, a = real_inputs
        result = load_inputs(kb, q, a)
        for query in result.queries:
            for doc_id in query.expected_doc_ids:
                assert doc_id in result.kb_by_id


class TestMalformedInput:
    def test_invalid_json_raises(self, broken_dir, real_inputs):
        _, q, a = real_inputs
        with pytest.raises(InputValidationError) as exc_info:
            load_inputs(os.path.join(broken_dir, "bad_json_kb.json"), q, a)
        assert "INVALID_JSON" in _codes(exc_info.value)

    def test_non_array_top_level_raises(self, broken_dir, real_inputs):
        _, q, a = real_inputs
        with pytest.raises(InputValidationError) as exc_info:
            load_inputs(os.path.join(broken_dir, "not_array_kb.json"), q, a)
        assert "NOT_AN_ARRAY" in _codes(exc_info.value)

    def test_duplicate_doc_id_raises(self, broken_dir, real_inputs):
        _, q, a = real_inputs
        with pytest.raises(InputValidationError) as exc_info:
            load_inputs(os.path.join(broken_dir, "dup_id_kb.json"), q, a)
        assert "DUPLICATE_ID" in _codes(exc_info.value)

    def test_missing_required_field_raises(self, broken_dir, real_inputs):
        kb, _, a = real_inputs
        with pytest.raises(InputValidationError) as exc_info:
            load_inputs(kb, os.path.join(broken_dir, "missing_field_queries.json"), a)
        assert "SCHEMA_INVALID" in _codes(exc_info.value)


class TestReferentialIntegrity:
    def test_dangling_doc_id_reference_raises(self, broken_dir, real_inputs):
        kb, _, a = real_inputs
        with pytest.raises(InputValidationError) as exc_info:
            load_inputs(kb, os.path.join(broken_dir, "dangling_doc_queries.json"), a)
        assert "DANGLING_DOC_ID" in _codes(exc_info.value)

    def test_dangling_query_id_reference_raises(self, broken_dir, real_inputs):
        kb, q, _ = real_inputs
        with pytest.raises(InputValidationError) as exc_info:
            load_inputs(kb, q, os.path.join(broken_dir, "dangling_query_answers.json"))
        assert "DANGLING_QUERY_ID" in _codes(exc_info.value)

    def test_empty_answer_map_raises(self, broken_dir, real_inputs):
        kb, q, _ = real_inputs
        with pytest.raises(InputValidationError) as exc_info:
            load_inputs(kb, q, os.path.join(broken_dir, "empty_answers_answers.json"))
        assert "SCHEMA_INVALID" in _codes(exc_info.value)


class TestFixtureSwapResilience:
    """The core contract: a schema-conformant fixture with completely
    different ids, question text, and variant *count* must load with zero
    special-casing.
    """

    def test_three_variants_with_unconventional_names_and_empty_constraints(self, broken_dir):
        result = load_inputs(
            os.path.join(broken_dir, "good_3variant_kb.json"),
            os.path.join(broken_dir, "good_3variant_queries.json"),
            os.path.join(broken_dir, "good_3variant_answers.json"),
        )
        assert result.variant_names == ("baseline", "v2", "v3_experimental")
        assert result.queries[0].must_include_any == []
        assert result.queries[0].must_not_claim == []
