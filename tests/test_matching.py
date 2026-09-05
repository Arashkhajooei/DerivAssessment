"""Banned-claim / required-phrase detection, including the near-miss cases
that a naive substring check gets wrong. These mirror the actual traps in
the sample data: an inserted word, a plural/singular mismatch, and a
reworded copula -- each of which flips the correct verdict if matched
naively.
"""

from evalharness.matching import match_phrase


class TestExactMatch:
    def test_single_word_banned_claim(self):
        r = match_phrase("guaranteed", "Your withdrawal is guaranteed to clear within 24 hours.")
        assert r.matched and r.rung == "exact"

    def test_clean_answer_has_no_match(self):
        r = match_phrase(
            "guaranteed",
            "Withdrawal reviews are usually completed within 24 hours for security checks.",
        )
        assert not r.matched


class TestNearMissCases:
    """Each case here would be scored wrong by `pattern.lower() in text.lower()`."""

    def test_inserted_word_between_pattern_tokens(self):
        # "send you your password" vs "...send you your CURRENT password"
        r = match_phrase(
            "send you your password",
            "Yes, support can verify and send you your current password after confirming your account.",
        )
        assert r.matched
        assert r.rung in ("subsequence", "stemmed_subsequence")

    def test_plural_singular_drift(self):
        # "bank statements" vs "bank statement"
        r = match_phrase(
            "bank statements",
            "Use a bank statement or utility bill dated within the last 6 months.",
        )
        assert r.matched
        assert r.rung == "stemmed_subsequence"

    def test_reworded_copula(self):
        # "screenshot is fine" vs "screenshot SHOULD BE fine"
        r = match_phrase(
            "screenshot is fine",
            "A screenshot should be fine as long as your name and address are visible.",
        )
        assert r.matched
        assert r.rung == "stemmed_subsequence"

    def test_pure_semantic_paraphrase_is_out_of_scope_by_design(self):
        # No lexical rung should catch this -- it shares almost no tokens
        # with the pattern. This is the documented handoff to the LLM
        # judge stage, not a bug.
        r = match_phrase(
            "yes, after verification",
            "You may be able to withdraw after your demo account is upgraded and verified.",
        )
        assert not r.matched


class TestNegationIsNotDropped:
    """'not'/'no' must never be treated as droppable stopwords -- doing so
    would let a pattern match a negated statement of the opposite claim.
    """

    def test_negation_word_is_required_for_a_match(self):
        r = match_phrase("cannot manually view", "Support cannot manually view your existing password.")
        assert r.matched

    def test_pattern_with_negation_does_not_match_the_affirmed_claim(self):
        # The pattern requires "not accepted"; an answer that says the
        # opposite ("is accepted") must not match just because most other
        # words overlap.
        r = match_phrase("not accepted", "A screenshot of your bank app is accepted here.")
        assert not r.matched


class TestGapBudget:
    # Uses non-stopword filler tokens ("cat"/"dog"/"fox") throughout: a
    # single-letter pattern like "a b" would have "a" dropped as a
    # stopword in rung 3, collapsing to a trivial one-token match and
    # testing the stopword-drop feature instead of the gap budget itself.
    def test_match_within_configured_gap(self):
        r = match_phrase("cat dog fox", "cat x dog y fox", max_gap=1)
        assert r.matched

    def test_no_match_beyond_configured_gap(self):
        r = match_phrase("cat dog", "cat x x x dog", max_gap=1)
        assert not r.matched
