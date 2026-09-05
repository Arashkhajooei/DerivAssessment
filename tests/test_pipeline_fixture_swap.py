"""Full-pipeline fixture-swap resilience: every stage (retrieval -> rule
checks -> judge -> taxonomy -> aggregation) run end-to-end against a
synthetic fixture with different ids, three variants under unconventional
names, and empty constraint arrays -- the same fixture used in
test_loader.py's TestFixtureSwapResilience, now proven all the way
through to a recommendation rather than just at the loading stage.

This is the closest thing to "the evaluator replaces the input files with
an equivalent fixture and reruns the pipeline" that a unit test can
exercise without shelling out to run.py.
"""

import os

from evalharness.aggregate import compute_recommendation
from evalharness.checks import evaluate_answer
from evalharness.config import load_config
from evalharness.judge import call_stub
from evalharness.loader import load_inputs
from evalharness.retrieval import BM25Index
from evalharness.taxonomy import build_failure_taxonomy

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_DIR = os.path.join(ROOT, "tests", "fixtures", "broken")


def test_three_variant_fixture_runs_end_to_end_without_hardcoded_assumptions():
    config = load_config("config.yaml")
    inputs = load_inputs(
        os.path.join(FIXTURE_DIR, "good_3variant_kb.json"),
        os.path.join(FIXTURE_DIR, "good_3variant_queries.json"),
        os.path.join(FIXTURE_DIR, "good_3variant_answers.json"),
    )
    assert inputs.variant_names == ("baseline", "v2", "v3_experimental")

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
    assert len(scores) == len(inputs.queries) * len(inputs.variant_names) == 3

    # Force the stub backend directly (no cache/live dependency) --
    # this fixture has no need to exercise the network path, only to
    # prove the pipeline is not hardcoded to two variants.
    llm_reviews = call_stub(inputs, scores, config)
    assert len(llm_reviews) == len(inputs.queries)
    for review in llm_reviews:
        assert review.winner in inputs.variant_names
        assert set(review.faithfulness) == set(inputs.variant_names)

    taxonomy = build_failure_taxonomy(inputs.queries, scores, llm_reviews, inputs.answers_by_query, config)
    assert len(taxonomy) == len(scores)

    recommendation = compute_recommendation(inputs.queries, scores, llm_reviews, taxonomy, config)
    # With empty must_include_any/must_not_claim (vacuously satisfied) and
    # no safety violations possible, every variant survives the gate --
    # the pipeline must still resolve to *some* verdict, not crash.
    assert all(not v.disqualified for v in recommendation.verdicts)
    assert recommendation.selected_variant in inputs.variant_names or recommendation.selected_variant is None
