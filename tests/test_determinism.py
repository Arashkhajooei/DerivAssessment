"""Determinism: running the deterministic stages twice on identical
inputs must produce byte-identical serialized output. This is the
concrete proof behind "retrieves evidence passages deterministically" and
"replayable evaluation pipeline" -- not just an assertion in the README.

The LLM judge stage is deliberately excluded here: its live backend is
inherently non-deterministic across real API calls (that's exactly why
the cache backend exists), so determinism for that stage is proven
separately in test_judge.py (identical cached input -> identical replayed
output), not here.
"""

import json

from evalharness.checks import evaluate_answer
from evalharness.config import load_config
from evalharness.loader import load_inputs
from evalharness.retrieval import BM25Index
from evalharness.schemas import RetrievalRecord, RetrievedPassage


def _run_retrieval_and_scores(config):
    inputs = load_inputs("kb.json", "queries.json", "candidate_answers.json")
    retrieval_cfg = config["retrieval"]
    index = BM25Index(inputs.kb, k1=retrieval_cfg["bm25_k1"], b=retrieval_cfg["bm25_b"])

    retrieval_records = []
    retrieved_by_query = {}
    for query in inputs.queries:
        passages = index.search(
            query.user_question, top_k=retrieval_cfg["top_k"], score_precision=retrieval_cfg["score_precision"]
        )
        retrieved_by_query[query.query_id] = passages
        retrieval_records.append(
            RetrievalRecord(
                query_id=query.query_id,
                retrieved=[
                    RetrievedPassage(doc_id=p.doc_id, score=p.score, title=p.title, text=p.text)
                    for p in passages
                ],
            )
        )

    score_records = [
        evaluate_answer(q, v, inputs.answers_by_query[q.query_id].answers[v], retrieved_by_query[q.query_id], config)
        for q in inputs.queries
        for v in inputs.variant_names
    ]
    return retrieval_records, score_records


def _canonical_json(records):
    return json.dumps([r.model_dump() for r in records], sort_keys=True)


class TestRetrievalAndScoringAreDeterministic:
    def test_two_independent_runs_produce_byte_identical_output(self):
        config = load_config("config.yaml")

        retrieval_1, scores_1 = _run_retrieval_and_scores(config)
        retrieval_2, scores_2 = _run_retrieval_and_scores(config)

        assert _canonical_json(retrieval_1) == _canonical_json(retrieval_2)
        assert _canonical_json(scores_1) == _canonical_json(scores_2)

    def test_ten_repeated_runs_all_agree(self):
        # A weaker form of determinism (e.g. depending on dict/set
        # iteration order or PYTHONHASHSEED) can pass twice by chance;
        # repeating it several times makes that far less likely.
        config = load_config("config.yaml")
        baseline_retrieval, baseline_scores = _run_retrieval_and_scores(config)
        baseline_r = _canonical_json(baseline_retrieval)
        baseline_s = _canonical_json(baseline_scores)

        for _ in range(9):
            retrieval, scores = _run_retrieval_and_scores(config)
            assert _canonical_json(retrieval) == baseline_r
            assert _canonical_json(scores) == baseline_s
