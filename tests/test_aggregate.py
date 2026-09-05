"""Tests for aggregation/recommendation logic, built on synthetic
AutomatedScoreRecord / LLMReviewRecord / FailureTaxonomyRecord data so
each scenario is isolated from what the real sample data happens to
contain. These are the cases the brief explicitly calls out: high-risk
failures must gate promotion, a clearer-but-less-safe variant must be
called out as a tradeoff, and a k-variant fixture must work unmodified.
"""

from evalharness.aggregate import compute_recommendation
from evalharness.config import load_config
from evalharness.schemas import AutomatedScoreRecord, FailureTaxonomyRecord, LLMReviewRecord, Query


def _config():
    return load_config("config.yaml")


def _score(query_id, variant, **overrides):
    base = dict(
        query_id=query_id,
        variant=variant,
        retrieval_hit=True,
        must_include_pass=True,
        must_not_claim_pass=True,
        grounding_score=0.9,
        risk_flags=["none"],
        notes="",
    )
    base.update(overrides)
    return AutomatedScoreRecord(**base)


def _review(query_id, winner, variants, faithfulness=4, clarity=4, overclaim=False):
    return LLMReviewRecord(
        query_id=query_id,
        winner=winner,
        faithfulness={v: faithfulness for v in variants},
        clarity={v: clarity for v in variants},
        overclaim_flags={v: overclaim for v in variants},
        justification="test",
    )


class TestSafetyGate:
    def test_high_risk_violation_disqualifies_even_with_higher_composite(self):
        # variant "b" is objectively "better" on grounding/clarity but
        # violates must_not_claim on a high-risk query -- it must lose.
        queries = [Query(query_id="Q1", user_question="?", risk_level="high")]
        scores = [
            _score("Q1", "a", grounding_score=0.6),
            _score("Q1", "b", must_not_claim_pass=False, grounding_score=0.99),
        ]
        reviews = [_review("Q1", "b", ["a", "b"], faithfulness=5, clarity=5)]
        taxonomy = [
            FailureTaxonomyRecord(query_id="Q1", variant="a", tags=[]),
            FailureTaxonomyRecord(query_id="Q1", variant="b", tags=["unsupported_claim", "policy_violation"]),
        ]
        rec = compute_recommendation(queries, scores, reviews, taxonomy, _config())
        assert rec.selected_variant == "a"

    def test_medium_risk_violation_does_not_trigger_the_gate(self):
        # Below the disqualifying severity (config default gates at
        # severity 3 == "high"), a must_not_claim failure should not gate
        # promotion by itself -- it still shows up as a composite penalty.
        queries = [Query(query_id="Q1", user_question="?", risk_level="medium")]
        scores = [
            _score("Q1", "a"),
            _score("Q1", "b", must_not_claim_pass=False),
        ]
        reviews = [_review("Q1", "a", ["a", "b"])]
        taxonomy = [
            FailureTaxonomyRecord(query_id="Q1", variant="a", tags=[]),
            FailureTaxonomyRecord(query_id="Q1", variant="b", tags=["unsupported_claim"]),
        ]
        rec = compute_recommendation(queries, scores, reviews, taxonomy, _config())
        b_verdict = next(v for v in rec.verdicts if v.variant == "b")
        assert not b_verdict.disqualified


class TestTradeoffSurfacing:
    def test_clearer_but_disqualified_variant_is_called_out(self):
        queries = [Query(query_id="Q1", user_question="?", risk_level="high")]
        scores = [
            _score("Q1", "a", grounding_score=0.5),
            _score("Q1", "b", must_not_claim_pass=False, grounding_score=0.9),
        ]
        reviews = [_review("Q1", "b", ["a", "b"], clarity=5)]
        # give "a" a lower clarity than "b" via a second review call is not
        # possible in one record; instead set a's clarity lower directly.
        reviews = [
            LLMReviewRecord(
                query_id="Q1", winner="b",
                faithfulness={"a": 3, "b": 4}, clarity={"a": 2, "b": 5},
                overclaim_flags={"a": False, "b": True}, justification="test",
            )
        ]
        taxonomy = [
            FailureTaxonomyRecord(query_id="Q1", variant="a", tags=[]),
            FailureTaxonomyRecord(query_id="Q1", variant="b", tags=["unsupported_claim", "policy_violation"]),
        ]
        rec = compute_recommendation(queries, scores, reviews, taxonomy, _config())
        assert rec.selected_variant == "a"
        assert any("clearer" in t.lower() or "clarity" in t.lower() for t in rec.tradeoffs)


class TestNoPromotionCases:
    def test_all_variants_disqualified_yields_no_promotion(self):
        queries = [Query(query_id="Q1", user_question="?", risk_level="high")]
        scores = [
            _score("Q1", "a", must_not_claim_pass=False),
            _score("Q1", "b", must_not_claim_pass=False),
        ]
        reviews = [_review("Q1", "a", ["a", "b"])]
        taxonomy = [
            FailureTaxonomyRecord(query_id="Q1", variant="a", tags=["unsupported_claim"]),
            FailureTaxonomyRecord(query_id="Q1", variant="b", tags=["unsupported_claim"]),
        ]
        rec = compute_recommendation(queries, scores, reviews, taxonomy, _config())
        assert rec.selected_variant is None

    def test_tiny_margin_between_survivors_yields_no_promotion(self):
        # No LLM review is supplied for this query, so the judge-derived
        # terms (faithfulness/clarity/winner_vote) contribute equally
        # (zero) to both variants, isolating the comparison to the tiny
        # grounding_score difference below the promotion margin threshold.
        queries = [Query(query_id="Q1", user_question="?", risk_level="low")]
        scores = [_score("Q1", "a", grounding_score=0.700), _score("Q1", "b", grounding_score=0.701)]
        reviews = []
        taxonomy = [
            FailureTaxonomyRecord(query_id="Q1", variant="a", tags=[]),
            FailureTaxonomyRecord(query_id="Q1", variant="b", tags=[]),
        ]
        rec = compute_recommendation(queries, scores, reviews, taxonomy, _config())
        assert rec.selected_variant is None
        assert rec.margin is not None and rec.margin < _config()["aggregation"]["min_margin_for_promotion"]


class TestKVariantSupport:
    def test_three_variants_one_disqualified_two_close(self):
        queries = [Query(query_id="Q1", user_question="?", risk_level="high")]
        scores = [
            _score("Q1", "baseline", grounding_score=0.5),
            _score("Q1", "v2", grounding_score=0.95),
            _score("Q1", "v3_bad", must_not_claim_pass=False, grounding_score=0.99),
        ]
        reviews = [_review("Q1", "v2", ["baseline", "v2", "v3_bad"])]
        taxonomy = [
            FailureTaxonomyRecord(query_id="Q1", variant="baseline", tags=[]),
            FailureTaxonomyRecord(query_id="Q1", variant="v2", tags=[]),
            FailureTaxonomyRecord(query_id="Q1", variant="v3_bad", tags=["unsupported_claim", "policy_violation"]),
        ]
        rec = compute_recommendation(queries, scores, reviews, taxonomy, _config())
        assert rec.selected_variant == "v2"
        v3 = next(v for v in rec.verdicts if v.variant == "v3_bad")
        assert v3.disqualified
