"""Text normalization utilities shared by retrieval and phrase matching.

A single normalization pipeline is used everywhere so that a token means
the same thing whether it comes from a KB passage, a question, an answer,
or a constraint pattern like `must_include_any`. Diverging tokenizers
between retrieval and matching would quietly weaken the "deterministic"
claim -- two components disagreeing on what a "word" is is a classic
source of hard-to-debug inconsistency.
"""

from __future__ import annotations

import re
from typing import List, Tuple

_TOKEN_RE = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")

# A small, fixed stopword list -- not exhaustive NLP tooling, just enough to
# stop trivial words from diluting BM25 term weights or breaking a
# subsequence match on a filler word. Deliberately excludes negation terms
# ("not", "no"): those are exactly the words that flip the meaning of a
# `must_include_any` / `must_not_claim` phrase in this domain (e.g.
# "cannot manually view", "screenshots are not accepted"), so dropping them
# as noise would be a correctness bug, not a simplification.
STOPWORDS = frozenset({
    "a", "an", "the", "of", "to", "in", "on", "at", "by", "for", "with",
    "as", "is", "are", "was", "were", "be", "been", "being",
    "and", "or", "but",
    "this", "that", "these", "those",
    "it", "its", "i", "you", "your", "we", "they", "he", "she",
    "do", "does", "did", "doing",
    "can", "could", "will", "would", "shall", "should", "may", "might", "must",
    "have", "has", "had",
    "if", "so", "than", "then",
})

# Minimal suffix-stripping stemmer. Deliberately not Porter/Snowball: those
# solve a much larger problem than this harness has. A short support-doc
# vocabulary is well served by a handful of suffix rules, applied longest
# and most-specific first.
_SUFFIX_RULES: Tuple[Tuple[str, str], ...] = (
    ("ies", "y"),
    ("ing", ""),
    ("ied", "y"),
    ("ed", ""),
    ("es", ""),
    ("s", ""),
    ("ly", ""),
)


def tokenize(text: str) -> List[str]:
    """Lowercase and split into word tokens, dropping punctuation.

    Deterministic: pure regex over `str.lower()`, no locale-dependent
    behaviour and no external tokenizer library or model.
    """
    return _TOKEN_RE.findall(text.lower())


def stem(token: str) -> str:
    """Apply a light suffix-stripping stem so that plural/verb-form
    variants ("statement"/"statements", "verify"/"verified") collapse to
    the same key. Never strips below 3 characters, so short words like
    "as" are left alone rather than mangled.
    """
    for suffix, replacement in _SUFFIX_RULES:
        if token.endswith(suffix) and len(token) - len(suffix) + len(replacement) >= 3:
            return token[: -len(suffix)] + replacement
    return token


def normalize_tokens(
    text: str, drop_stopwords: bool = False, apply_stem: bool = False
) -> List[str]:
    """Tokenize, then optionally drop stopwords and/or stem. The two flags
    are independent so callers (retrieval vs. matching) opt into only what
    they need.
    """
    tokens = tokenize(text)
    if drop_stopwords:
        tokens = [t for t in tokens if t not in STOPWORDS]
    if apply_stem:
        tokens = [stem(t) for t in tokens]
    return tokens
