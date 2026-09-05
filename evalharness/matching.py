"""Phrase-matching ladder used for the must_include_any / must_not_claim
checks.

    Rung 1 (exact):              plain normalized substring match.
    Rung 2 (subsequence):         pattern tokens appear in order in the
                                  answer, with up to `max_gap` unmatched
                                  tokens between consecutive pattern words
                                  (handles an inserted word, e.g. "send you
                                  your password" vs "send you your CURRENT
                                  password").
    Rung 3 (stemmed_subsequence): like rung 2, but pattern stopwords may be
                                  dropped and both sides are stemmed
                                  (handles plural/verb-form drift and minor
                                  copula rewording: "bank statements" vs
                                  "bank statement", "screenshot is fine" vs
                                  "screenshot SHOULD BE fine").

All three rungs are pure, deterministic functions of their inputs -- no
model call, no randomness. A pattern that means the same thing as the
answer text but shares almost no tokens with it (pure paraphrase) is out of
scope for this ladder by design: that is exactly the kind of judgment the
LLM review stage exists to make, and is a documented limitation of this
deterministic layer, not an oversight.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from evalharness.text import STOPWORDS, stem, tokenize


@dataclass(frozen=True)
class MatchResult:
    matched: bool
    rung: Optional[str]  # "exact" | "subsequence" | "stemmed_subsequence" | None


def _exact_match(pattern: str, text: str) -> bool:
    return pattern.lower().strip() in text.lower()


def _subsequence_match(pattern_tokens: List[str], text_tokens: List[str], max_gap: int) -> bool:
    """True if pattern_tokens appear in text_tokens in order, with no more
    than `max_gap` unmatched text tokens between consecutive pattern
    tokens. A bounded gap models "a word or two got inserted" without
    degrading into an unordered bag-of-words match, which would over-match
    on any answer sharing the same vocabulary in a different order.
    """
    if not pattern_tokens:
        return False
    n = len(text_tokens)
    for start in range(n):
        if text_tokens[start] != pattern_tokens[0]:
            continue
        cursor = start
        pi = 1
        ok = True
        while pi < len(pattern_tokens):
            found_at = None
            # lookahead - 1 == number of unmatched tokens skipped (the gap)
            for lookahead in range(1, max_gap + 2):
                idx = cursor + lookahead
                if idx >= n:
                    break
                if text_tokens[idx] == pattern_tokens[pi]:
                    found_at = idx
                    break
            if found_at is None:
                ok = False
                break
            cursor = found_at
            pi += 1
        if ok:
            return True
    return False


def match_phrase(pattern: str, text: str, max_gap: int = 2) -> MatchResult:
    """Try each rung in increasing tolerance; return the first that hits."""
    if not pattern or not pattern.strip():
        return MatchResult(matched=False, rung=None)

    if _exact_match(pattern, text):
        return MatchResult(matched=True, rung="exact")

    pattern_tokens = tokenize(pattern)
    text_tokens = tokenize(text)
    if _subsequence_match(pattern_tokens, text_tokens, max_gap):
        return MatchResult(matched=True, rung="subsequence")

    pattern_tokens_stemmed = [stem(t) for t in pattern_tokens if t not in STOPWORDS]
    text_tokens_stemmed = [stem(t) for t in text_tokens]
    if pattern_tokens_stemmed and _subsequence_match(
        pattern_tokens_stemmed, text_tokens_stemmed, max_gap
    ):
        return MatchResult(matched=True, rung="stemmed_subsequence")

    return MatchResult(matched=False, rung=None)


def match_any(patterns: List[str], text: str, max_gap: int = 2) -> List[MatchResult]:
    """Match each pattern independently against `text`. Callers decide how
    to combine results: must_include_any treats any match as satisfying
    the constraint; must_not_claim treats any match as a violation.
    """
    return [match_phrase(p, text, max_gap=max_gap) for p in patterns]
