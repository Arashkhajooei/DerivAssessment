"""Deterministic local retrieval over the knowledge base.

Implements Okapi BM25 over each document's `title + text`. No network
calls, no embeddings, no external service -- the brief requires retrieval
"implemented in code using a simple, local approach such as BM25, TF-IDF,
token overlap, or equivalent," and separately bans external calls for
retrieval or knowledge lookup. BM25 is simple, well-understood, and fully
local.

Determinism guarantees:
  - tokenization is a pure function (evalharness.text), no external state,
    no locale dependence
  - scores are rounded to a fixed precision before being returned, so
    floating-point representation differences across platforms cannot
    change the serialized output
  - ties are broken by doc_id (ascending), so two passages with identical
    scores always sort the same way regardless of dict/set iteration
    order or which Python version processed them
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Tuple

from evalharness.schemas import KBDocument
from evalharness.text import normalize_tokens


@dataclass(frozen=True)
class ScoredPassage:
    doc_id: str
    score: float
    title: str
    text: str


class BM25Index:
    """A small in-memory BM25 index, built once per run and queried per
    question. Tokenization drops stopwords and stems, using the same
    normalization pipeline as the phrase-matching ladder, so retrieval and
    matching agree on what counts as "the same word."
    """

    def __init__(self, documents: List[KBDocument], k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.documents = documents
        self._doc_by_id: Dict[str, KBDocument] = {d.doc_id: d for d in documents}

        self._doc_tokens: Dict[str, List[str]] = {
            doc.doc_id: normalize_tokens(
                "{} {}".format(doc.title, doc.text), drop_stopwords=True, apply_stem=True
            )
            for doc in documents
        }
        self._doc_len: Dict[str, int] = {
            doc_id: len(toks) for doc_id, toks in self._doc_tokens.items()
        }
        self._avgdl: float = (
            sum(self._doc_len.values()) / len(self._doc_len) if self._doc_len else 0.0
        )

        self._term_freqs: Dict[str, Dict[str, int]] = {}
        for doc_id, toks in self._doc_tokens.items():
            tf: Dict[str, int] = {}
            for tok in toks:
                tf[tok] = tf.get(tok, 0) + 1
            self._term_freqs[doc_id] = tf

        self._doc_freq: Dict[str, int] = {}
        for tf in self._term_freqs.values():
            for term in tf:
                self._doc_freq[term] = self._doc_freq.get(term, 0) + 1
        self._n_docs = len(documents)

    def _idf(self, term: str) -> float:
        n_t = self._doc_freq.get(term, 0)
        # The "+1" inside the log (BM25+ style smoothing) keeps IDF
        # non-negative even for a term appearing in most of the corpus,
        # which matters here precisely because the corpus is tiny.
        return math.log(1.0 + (self._n_docs - n_t + 0.5) / (n_t + 0.5))

    def _score_one(self, query_tokens: List[str], doc_id: str) -> float:
        tf = self._term_freqs[doc_id]
        dl = self._doc_len[doc_id]
        norm = self.b * (dl / self._avgdl) if self._avgdl else 0.0
        total = 0.0
        for term in query_tokens:
            f = tf.get(term, 0)
            if f == 0:
                continue
            idf = self._idf(term)
            denom = f + self.k1 * (1 - self.b + norm)
            total += idf * (f * (self.k1 + 1)) / denom
        return total

    def search(self, query_text: str, top_k: int, score_precision: int = 4) -> List[ScoredPassage]:
        """Return up to `top_k` passages for `query_text`, highest score
        first, ties broken by ascending doc_id. Returns fewer than
        `top_k` if the knowledge base itself has fewer documents -- this
        is not an error, just a small corpus.
        """
        query_tokens = normalize_tokens(query_text, drop_stopwords=True, apply_stem=True)

        scored: List[Tuple[str, float]] = [
            (doc_id, round(self._score_one(query_tokens, doc_id), score_precision))
            for doc_id in self._doc_tokens
        ]
        scored.sort(key=lambda pair: (-pair[1], pair[0]))

        results: List[ScoredPassage] = []
        for doc_id, s in scored[:top_k]:
            doc = self._doc_by_id[doc_id]
            results.append(ScoredPassage(doc_id=doc_id, score=s, title=doc.title, text=doc.text))
        return results
