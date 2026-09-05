"""Tests for the judge stage: schema validation and backend resolution.

The live backend cannot be exercised against the real Anthropic API in
this environment (no key). These tests instead monkeypatch
`evalharness.judge.call_live` to return canned, schema-shaped responses,
which lets the validation, retry, and cache-replay logic be verified
without any network access -- everything downstream of "a response came
back" is tested for real.
"""

import json
import os

import pytest

from evalharness.checks import evaluate_answer
from evalharness.config import load_config
from evalharness.loader import load_inputs
from evalharness.retrieval import BM25Index
import evalharness.judge as judge


def _build_pipeline_state(config):
    inputs = load_inputs("kb.json", "queries.json", "candidate_answers.json")
    retrieval_cfg = config["retrieval"]
    index = BM25Index(inputs.kb, k1=retrieval_cfg["bm25_k1"], b=retrieval_cfg["bm25_b"])
    retrieved_by_query = {
        q.query_id: index.search(
            q.user_question, top_k=retrieval_cfg["top_k"], score_precision=retrieval_cfg["score_precision"]
        )
        for q in inputs.queries
    }
    scores = [
        evaluate_answer(q, v, inputs.answers_by_query[q.query_id].answers[v], retrieved_by_query[q.query_id], config)
        for q in inputs.queries
        for v in inputs.variant_names
    ]
    return inputs, retrieved_by_query, scores


@pytest.fixture
def config(tmp_path):
    cfg = load_config("config.yaml")
    # Isolate the call log so tests never touch the repo's real llm_calls.jsonl.
    cfg["paths"] = dict(cfg["paths"])
    cfg["paths"]["llm_calls"] = str(tmp_path / "llm_calls.jsonl")
    return cfg


def _well_formed_response(inputs, variant_names_by_query, score_min=3, score_max=3):
    reviews = []
    for q in inputs.queries:
        variants = variant_names_by_query.get(q.query_id)
        if not variants:
            continue
        reviews.append(
            {
                "query_id": q.query_id,
                "winner": variants[0],
                "faithfulness": {v: score_min for v in variants},
                "clarity": {v: score_max for v in variants},
                "overclaim_flags": {v: False for v in variants},
                "justification": "Mocked justification for testing.",
            }
        )
    return {"reviews": reviews}


class TestValidateReviews:
    def test_accepts_well_formed_payload(self, config):
        inputs, _, _ = _build_pipeline_state(config)
        variant_names_by_query = {q.query_id: sorted(inputs.answers_by_query[q.query_id].answers) for q in inputs.queries}
        payload = _well_formed_response(inputs, variant_names_by_query)
        records, errors = judge.validate_reviews(payload, inputs, score_min=1, score_max=5)
        assert errors == []
        assert len(records) == len(inputs.queries)

    def test_rejects_unknown_winner(self, config):
        inputs, _, _ = _build_pipeline_state(config)
        variant_names_by_query = {q.query_id: sorted(inputs.answers_by_query[q.query_id].answers) for q in inputs.queries}
        payload = _well_formed_response(inputs, variant_names_by_query)
        payload["reviews"][0]["winner"] = "not_a_real_variant"
        records, errors = judge.validate_reviews(payload, inputs, score_min=1, score_max=5)
        assert records == []
        assert any("not in the variant set" in e for e in errors)

    def test_rejects_out_of_range_score(self, config):
        inputs, _, _ = _build_pipeline_state(config)
        variant_names_by_query = {q.query_id: sorted(inputs.answers_by_query[q.query_id].answers) for q in inputs.queries}
        payload = _well_formed_response(inputs, variant_names_by_query)
        first_variant = payload["reviews"][0]["winner"]
        payload["reviews"][0]["faithfulness"][first_variant] = 999
        records, errors = judge.validate_reviews(payload, inputs, score_min=1, score_max=5)
        assert records == []
        assert any("must be an integer in" in e for e in errors)

    def test_rejects_mismatched_variant_keys(self, config):
        inputs, _, _ = _build_pipeline_state(config)
        variant_names_by_query = {q.query_id: sorted(inputs.answers_by_query[q.query_id].answers) for q in inputs.queries}
        payload = _well_formed_response(inputs, variant_names_by_query)
        payload["reviews"][0]["faithfulness"] = {"totally_unknown_variant": 3}
        records, errors = judge.validate_reviews(payload, inputs, score_min=1, score_max=5)
        assert records == []
        assert any("keys must exactly equal variant set" in e for e in errors)

    def test_rejects_missing_query_coverage(self, config):
        inputs, _, _ = _build_pipeline_state(config)
        variant_names_by_query = {q.query_id: sorted(inputs.answers_by_query[q.query_id].answers) for q in inputs.queries}
        payload = _well_formed_response(inputs, variant_names_by_query)
        payload["reviews"].pop()
        records, errors = judge.validate_reviews(payload, inputs, score_min=1, score_max=5)
        assert records == []
        assert any("missing review" in e for e in errors)


class TestStubBackend:
    def test_stub_winner_matches_the_safer_variant(self, config):
        inputs, retrieved_by_query, scores = _build_pipeline_state(config)
        records = judge.call_stub(inputs, scores, config)
        by_qid = {r.query_id: r for r in records}
        # prompt_a is clean on every query in the real fixtures; prompt_b
        # violates must_not_claim on Q1-Q3. The stub must always prefer
        # the variant with no banned-claim violation.
        for qid in ("Q1", "Q2", "Q3"):
            assert by_qid[qid].winner == "prompt_a"


class TestBackendResolution:
    def test_falls_through_to_stub_when_live_unavailable(self, config):
        inputs, retrieved_by_query, scores = _build_pipeline_state(config)
        records, backend = judge.run_judge_stage(inputs, retrieved_by_query, scores, config)
        assert backend == "stub"
        assert len(records) == len(inputs.queries)
        # Both the failed live attempt and the stub fallback are logged.
        with open(config["paths"]["llm_calls"]) as fh:
            lines = [json.loads(l) for l in fh if l.strip()]
        assert any(e["backend"] == "live" and not e["parsed_ok"] for e in lines)
        assert any(e["backend"] == "stub" and e["parsed_ok"] for e in lines)

    def test_uses_live_when_mocked_available(self, config, monkeypatch):
        inputs, retrieved_by_query, scores = _build_pipeline_state(config)
        variant_names_by_query = {q.query_id: sorted(inputs.answers_by_query[q.query_id].answers) for q in inputs.queries}
        canned = _well_formed_response(inputs, variant_names_by_query)

        def fake_call_live(system_prompt, user_prompt, cfg):
            return json.dumps(canned), 42, {"input_tokens": 100, "output_tokens": 50}

        monkeypatch.setattr(judge, "call_live", fake_call_live)
        records, backend = judge.run_judge_stage(inputs, retrieved_by_query, scores, config)
        assert backend == "live"
        assert len(records) == len(inputs.queries)

    def test_replays_from_cache_on_second_call(self, config, monkeypatch):
        inputs, retrieved_by_query, scores = _build_pipeline_state(config)
        variant_names_by_query = {q.query_id: sorted(inputs.answers_by_query[q.query_id].answers) for q in inputs.queries}
        canned = _well_formed_response(inputs, variant_names_by_query)
        call_count = {"n": 0}

        def fake_call_live(system_prompt, user_prompt, cfg):
            call_count["n"] += 1
            return json.dumps(canned), 42, None

        monkeypatch.setattr(judge, "call_live", fake_call_live)
        _, backend1 = judge.run_judge_stage(inputs, retrieved_by_query, scores, config)
        _, backend2 = judge.run_judge_stage(inputs, retrieved_by_query, scores, config)

        assert backend1 == "live"
        assert backend2 == "cache"
        assert call_count["n"] == 1  # the live backend was invoked only once

    def test_repairs_and_then_falls_back_to_stub_on_persistent_invalid_output(self, config, monkeypatch):
        inputs, retrieved_by_query, scores = _build_pipeline_state(config)

        def always_bad_call_live(system_prompt, user_prompt, cfg):
            return json.dumps({"reviews": []}), 10, None

        monkeypatch.setattr(judge, "call_live", always_bad_call_live)
        records, backend = judge.run_judge_stage(inputs, retrieved_by_query, scores, config)
        assert backend == "stub"
        with open(config["paths"]["llm_calls"]) as fh:
            lines = [json.loads(l) for l in fh if l.strip()]
        live_attempts = [e for e in lines if e["backend"] == "live"]
        # max_retries=1 in config.yaml -> 2 live attempts total, both invalid.
        assert len(live_attempts) == config["judge"]["max_retries"] + 1
        assert all(not e["parsed_ok"] for e in live_attempts)


class TestProviderDispatch:
    """The live backend dispatches on `judge.provider`. These tests cover
    the dispatch and cache-key behaviour without making any network call.
    """

    def test_unknown_provider_is_unavailable_not_a_crash(self, config):
        # An unrecognised provider must fall through to the next backend
        # (raising LiveBackendUnavailable), never take down the pipeline.
        config["judge"]["provider"] = "not_a_real_provider"
        with pytest.raises(judge.LiveBackendUnavailable) as exc_info:
            judge.call_live("system", "user", config)
        assert "unknown judge provider" in str(exc_info.value)

    def test_missing_openai_key_is_unavailable(self, config, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        config["judge"]["provider"] = "openai"
        with pytest.raises(judge.LiveBackendUnavailable) as exc_info:
            judge.call_live("system", "user", config)
        assert "OPENAI_API_KEY" in str(exc_info.value)

    def test_missing_anthropic_key_is_unavailable(self, config, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        config["judge"]["provider"] = "anthropic"
        with pytest.raises(judge.LiveBackendUnavailable) as exc_info:
            judge.call_live("system", "user", config)
        # Either the SDK is absent or the key is -- both are "unavailable",
        # which is the behaviour that matters here.
        assert "not installed" in str(exc_info.value) or "ANTHROPIC_API_KEY" in str(exc_info.value)

    def test_provider_is_part_of_the_cache_key(self):
        # Two providers can expose the same model name; a cached answer
        # from one must never be replayed as the other's.
        args = ("gpt-4o-mini", 0, 4096, "identical prompt")
        assert judge.compute_prompt_hash("openai", *args) != judge.compute_prompt_hash("anthropic", *args)

    def test_same_inputs_hash_identically(self):
        args = ("openai", "gpt-4o-mini", 0, 4096, "identical prompt")
        assert judge.compute_prompt_hash(*args) == judge.compute_prompt_hash(*args)

    def test_prompt_change_changes_the_hash(self):
        base = ("openai", "gpt-4o-mini", 0, 4096)
        assert judge.compute_prompt_hash(*base, "prompt one") != judge.compute_prompt_hash(*base, "prompt two")
