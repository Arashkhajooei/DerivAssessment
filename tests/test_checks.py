"""Grounding score behavior, tested against synthetic evidence so each
assertion describes the *rule* -- precision from token overlap, a bonus
for verbatim quoting, a penalty for invented numbers -- independent of
whatever the real sample data happens to score.
"""

from evalharness.checks import check_must_include, check_must_not_claim, compute_grounding_score
from evalharness.retrieval import ScoredPassage

_PRECISION_WEIGHT = 0.85
_MIN_QUOTE_SPAN = 4
_QUOTE_BONUS = 0.15
_NUMERIC_PENALTY = 0.25
_DECIMALS = 4


def _passage(text, doc_id="D1", title="Doc"):
    return ScoredPassage(doc_id=doc_id, score=1.0, title=title, text=text)


def _score(answer_text, evidence_passages):
    return compute_grounding_score(
        answer_text,
        evidence_passages,
        precision_weight=_PRECISION_WEIGHT,
        min_quote_span=_MIN_QUOTE_SPAN,
        quote_bonus=_QUOTE_BONUS,
        numeric_penalty=_NUMERIC_PENALTY,
        precision_decimals=_DECIMALS,
    )


class TestPrecisionComponent:
    def test_fully_overlapping_answer_scores_high(self):
        evidence = [_passage("Withdrawals complete within 24 hours for security checks.")]
        score, details = _score("Withdrawals complete within 24 hours for security checks.", evidence)
        assert details["precision"] == 1.0
        assert score > 0.9  # precision(1.0)*0.85 + quote_bonus(0.15) = 1.0, clamped

    def test_completely_unrelated_answer_scores_low(self):
        evidence = [_passage("Withdrawals complete within 24 hours for security checks.")]
        score, details = _score("Bananas are a good source of potassium.", evidence)
        assert details["precision"] < 0.3
        assert score < 0.3

    def test_empty_answer_has_zero_precision(self):
        evidence = [_passage("Some evidence text.")]
        score, details = _score("", evidence)
        assert details["precision"] == 0.0
        assert score == 0.0


class TestQuoteBonus:
    def test_verbatim_quote_span_earns_the_bonus(self):
        evidence = [_passage("Screenshots are not accepted as proof of address here.")]
        score_with_quote, details_with = _score(
            "Screenshots are not accepted as proof.", evidence
        )
        assert details_with["has_quote_span"] is True

    def test_no_verbatim_span_below_min_length_gets_no_bonus(self):
        evidence = [_passage("Screenshots are not accepted as proof of address here.")]
        # Rephrased so no 4+ token run matches verbatim.
        score, details = _score("We do not accept screenshots as proof.", evidence)
        assert details["has_quote_span"] is False


class TestNumericPenalty:
    def test_number_absent_from_evidence_is_flagged_and_penalized(self):
        evidence = [_passage("Reviews complete within 24 hours.")]
        score_bad, details_bad = _score("Your review will complete within 48 hours.", evidence)
        assert "48" in details_bad["unsupported_numbers"]

        score_good, details_good = _score("Your review will complete within 24 hours.", evidence)
        assert details_good["unsupported_numbers"] == []
        assert score_good > score_bad

    def test_penalty_never_pushes_score_below_zero(self):
        evidence = [_passage("No numbers here at all.")]
        score, _ = _score("This costs 999 dollars and takes 42 days with a 7 day grace period.", evidence)
        assert score >= 0.0


class TestMustIncludeAndMustNotClaimEmptyConstraints:
    def test_empty_must_include_list_is_vacuously_satisfied(self):
        passed, matched = check_must_include([], "any answer text at all", max_gap=2)
        assert passed is True
        assert matched == []

    def test_empty_must_not_claim_list_is_vacuously_satisfied(self):
        passed, violations = check_must_not_claim([], "any answer text at all", max_gap=2)
        assert passed is True
        assert violations == []
