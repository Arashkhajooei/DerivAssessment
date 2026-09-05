"""Aggregation: combine retrieval, rule-based checks, the LLM review, and
the failure taxonomy -- reading only already-stored artifacts -- into one
promotion recommendation.

This module only consumes the Pydantic records the earlier stages already
produced (never raw answer text, never the judge itself), which is a
direct expression of the brief's constraint that "the final promotion
recommendation must be computed in code from stored outputs": running
this module again against yesterday's retrieval.json / automated_scores.json
/ llm_review.json / failure_taxonomy.json reproduces the same
recommendation without needing anything else.

Aggregation logic, in order:
  1. Safety gate -- any variant with a must_not_claim violation on a
     query whose risk_level meets config.gates.disqualify_must_not_claim_
     at_severity is disqualified outright. Disqualification is a veto,
     not a weighted penalty: no composite score can outweigh it.
  2. Composite score -- a weighted blend (config.aggregation.weights) of
     rule-based checks, grounding, and judge scores, averaged per query,
     computed for every variant (including disqualified ones, so the
     tradeoff section below can still compare them).
  3. Selection among the variants that pass the gate:
       - zero survivors  -> no promotion recommended
       - one survivor    -> promote it
       - multiple survivors -> promote the highest composite score, but
         only if the margin over the runner-up clears
         config.aggregation.min_margin_for_promotion; otherwise the
         evidence is treated as insufficient to prefer one over the other
  4. Tradeoff surfacing -- if a disqualified variant scored higher on
     judged clarity than the selected/best-surviving variant, that is
     called out explicitly, matching the brief's requirement to "explain
     tradeoffs if one variant is clearer but less safe."
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from evalharness.schemas import AutomatedScoreRecord, FailureTaxonomyRecord, LLMReviewRecord, Query


@dataclass
class VariantVerdict:
    variant: str
    disqualified: bool
    disqualifying_reasons: List[str]
    composite_score: float
    mean_clarity: Optional[float]
    mean_faithfulness: Optional[float]
    judge_wins: int
    failure_tag_counts: Dict[str, int]


@dataclass
class Recommendation:
    selected_variant: Optional[str]
    verdicts: List[VariantVerdict]
    reasons: List[str]
    tradeoffs: List[str]
    margin: Optional[float]


def _severity(risk_level: str, risk_cfg: dict) -> int:
    return risk_cfg["levels"].get(risk_level, risk_cfg["unknown_severity"])


def compute_recommendation(
    queries: List[Query],
    automated_scores: List[AutomatedScoreRecord],
    llm_reviews: List[LLMReviewRecord],
    failure_taxonomy: List[FailureTaxonomyRecord],
    config: dict,
) -> Recommendation:
    reviews_by_qid = {r.query_id: r for r in llm_reviews}
    weights = config["aggregation"]["weights"]
    judge_cfg = config["judge"]
    risk_cfg = config["risk"]
    gates_cfg = config["gates"]
    precision = config["aggregation"]["score_precision"]

    variants = sorted({s.variant for s in automated_scores})
    scores_by_key = {(s.query_id, s.variant): s for s in automated_scores}
    taxonomy_by_key = {(t.query_id, t.variant): t for t in failure_taxonomy}
    score_range = max(1, judge_cfg["score_max"] - judge_cfg["score_min"])

    disqualified: Dict[str, List[str]] = {v: [] for v in variants}
    composite_terms: Dict[str, List[float]] = {v: [] for v in variants}
    clarity_values: Dict[str, List[int]] = {v: [] for v in variants}
    faithfulness_values: Dict[str, List[int]] = {v: [] for v in variants}
    judge_wins: Dict[str, int] = {v: 0 for v in variants}
    tag_counts: Dict[str, Dict[str, int]] = {v: {} for v in variants}

    for query in queries:
        query_variants = [v for v in variants if (query.query_id, v) in scores_by_key]
        if not query_variants:
            continue
        severity = _severity(query.risk_level, risk_cfg)
        review = reviews_by_qid.get(query.query_id)

        for v in query_variants:
            score = scores_by_key[(query.query_id, v)]
            taxonomy = taxonomy_by_key.get((query.query_id, v))

            if (not score.must_not_claim_pass) and severity >= gates_cfg["disqualify_must_not_claim_at_severity"]:
                disqualified[v].append(
                    "{}: banned claim on a {}-risk query".format(query.query_id, query.risk_level)
                )

            faithfulness_norm = clarity_norm = 0.0
            if review is not None and v in review.faithfulness:
                faithfulness_values[v].append(review.faithfulness[v])
                faithfulness_norm = (review.faithfulness[v] - judge_cfg["score_min"]) / score_range
            if review is not None and v in review.clarity:
                clarity_values[v].append(review.clarity[v])
                clarity_norm = (review.clarity[v] - judge_cfg["score_min"]) / score_range
            winner_vote = 1.0 if review is not None and review.winner == v else 0.0
            if winner_vote:
                judge_wins[v] += 1

            contribution = (
                weights["retrieval_hit"] * int(score.retrieval_hit)
                + weights["must_include_pass"] * int(score.must_include_pass)
                + weights["must_not_claim_pass"] * int(score.must_not_claim_pass)
                + weights["grounding_score"] * score.grounding_score
                + weights["judge_faithfulness"] * faithfulness_norm
                + weights["judge_clarity"] * clarity_norm
                + weights["judge_winner_vote"] * winner_vote
            )
            composite_terms[v].append(contribution)

            if taxonomy:
                for tag in taxonomy.tags:
                    tag_counts[v][tag] = tag_counts[v].get(tag, 0) + 1

    verdicts: List[VariantVerdict] = []
    for v in variants:
        terms = composite_terms[v]
        composite = round(sum(terms) / len(terms), precision) if terms else 0.0
        mean_clarity = round(sum(clarity_values[v]) / len(clarity_values[v]), 2) if clarity_values[v] else None
        mean_faithfulness = (
            round(sum(faithfulness_values[v]) / len(faithfulness_values[v]), 2)
            if faithfulness_values[v]
            else None
        )
        verdicts.append(
            VariantVerdict(
                variant=v,
                disqualified=bool(disqualified[v]),
                disqualifying_reasons=disqualified[v],
                composite_score=composite,
                mean_clarity=mean_clarity,
                mean_faithfulness=mean_faithfulness,
                judge_wins=judge_wins[v],
                failure_tag_counts=tag_counts[v],
            )
        )

    candidates = [vd for vd in verdicts if not vd.disqualified]
    disqualified_verdicts = [vd for vd in verdicts if vd.disqualified]

    reasons: List[str] = []
    for vd in disqualified_verdicts:
        reasons.append("'{}' is disqualified: {}".format(vd.variant, "; ".join(vd.disqualifying_reasons)))

    selected: Optional[str] = None
    margin: Optional[float] = None

    if not candidates:
        reasons.append("No variant passes the safety gate -- no promotion is recommended.")
    elif len(candidates) == 1:
        selected = candidates[0].variant
        reasons.append("'{}' is the only variant that passes the safety gate.".format(selected))
    else:
        ranked = sorted(candidates, key=lambda vd: (-vd.composite_score, vd.variant))
        top, runner_up = ranked[0], ranked[1]
        margin = round(top.composite_score - runner_up.composite_score, precision)
        if margin < config["aggregation"]["min_margin_for_promotion"]:
            reasons.append(
                "No variant is promoted: the composite margin between '{}' ({}) and '{}' "
                "({}) is {}, below the configured threshold of {} -- insufficient evidence "
                "to prefer one over the other.".format(
                    top.variant, top.composite_score, runner_up.variant, runner_up.composite_score,
                    margin, config["aggregation"]["min_margin_for_promotion"],
                )
            )
        else:
            selected = top.variant
            reasons.append(
                "'{}' has the highest composite score among safety-gate survivors ({} vs {} "
                "for '{}', margin {}).".format(
                    top.variant, top.composite_score, runner_up.composite_score, runner_up.variant, margin
                )
            )

    tradeoffs: List[str] = []
    comparison_target = selected or (candidates[0].variant if candidates else None)
    if comparison_target:
        target_verdict = next(vd for vd in verdicts if vd.variant == comparison_target)
        for vd in disqualified_verdicts:
            if (
                vd.mean_clarity is not None
                and target_verdict.mean_clarity is not None
                and vd.mean_clarity > target_verdict.mean_clarity
            ):
                tradeoffs.append(
                    "'{}' scored higher on judged clarity than '{}' ({} vs {}) but is "
                    "disqualified on safety grounds -- clarity does not offset a high-risk "
                    "must_not_claim violation under this harness's gating rule.".format(
                        vd.variant, comparison_target, vd.mean_clarity, target_verdict.mean_clarity
                    )
                )

    return Recommendation(
        selected_variant=selected,
        verdicts=verdicts,
        reasons=reasons,
        tradeoffs=tradeoffs,
        margin=margin,
    )


def render_recommendation_markdown(
    recommendation: Recommendation,
    queries: List[Query],
    automated_scores: List[AutomatedScoreRecord],
    llm_reviews: List[LLMReviewRecord],
    judge_backend: str,
    config: dict,
) -> str:
    """Render `recommendation.md`: selected variant, summary table, top
    reasons, per-query detail, and known limitations -- everything the
    brief's "Final Promotion Recommendation" section requires.
    """
    scores_by_key = {(s.query_id, s.variant): s for s in automated_scores}
    reviews_by_qid = {r.query_id: r for r in llm_reviews}
    variants = sorted({vd.variant for vd in recommendation.verdicts})

    lines: List[str] = ["# Promotion Recommendation", ""]
    if recommendation.selected_variant:
        lines.append("**Selected variant: `{}`**".format(recommendation.selected_variant))
    else:
        lines.append("**No variant is recommended for promotion.**")
    lines.append("")

    lines.append("## Top reasons")
    lines.append("")
    for r in recommendation.reasons:
        lines.append("- {}".format(r))
    lines.append("")

    if recommendation.tradeoffs:
        lines.append("## Tradeoffs")
        lines.append("")
        for t in recommendation.tradeoffs:
            lines.append("- {}".format(t))
        lines.append("")

    lines.append("## Variant summary")
    lines.append("")
    lines.append(
        "| Variant | Disqualified | Composite score | Mean judge faithfulness | "
        "Mean judge clarity | Judge wins | Failure tags |"
    )
    lines.append("|---|---|---|---|---|---|---|")
    for vd in sorted(recommendation.verdicts, key=lambda v: v.variant):
        tag_summary = (
            ", ".join("{}={}".format(k, v) for k, v in sorted(vd.failure_tag_counts.items())) or "none"
        )
        lines.append(
            "| `{}` | {} | {} | {} | {} | {} | {} |".format(
                vd.variant,
                ("yes -- " + "; ".join(vd.disqualifying_reasons)) if vd.disqualified else "no",
                vd.composite_score,
                vd.mean_faithfulness if vd.mean_faithfulness is not None else "n/a",
                vd.mean_clarity if vd.mean_clarity is not None else "n/a",
                vd.judge_wins,
                tag_summary,
            )
        )
    lines.append("")

    lines.append("## Per-query detail")
    lines.append("")
    lines.append(
        "| Query | Risk | Variant | Retrieval hit | Must-include | Must-not-claim | "
        "Grounding | Judge winner |"
    )
    lines.append("|---|---|---|---|---|---|---|---|")
    for q in queries:
        review = reviews_by_qid.get(q.query_id)
        for v in variants:
            score = scores_by_key.get((q.query_id, v))
            if score is None:
                continue
            lines.append(
                "| {} | {} | `{}` | {} | {} | {} | {} | {} |".format(
                    q.query_id, q.risk_level, v, score.retrieval_hit, score.must_include_pass,
                    score.must_not_claim_pass, score.grounding_score,
                    review.winner if review else "n/a",
                )
            )
    lines.append("")

    lines.append("## Known limitations of this evaluation harness")
    lines.append("")
    lines.append("- Judge backend used for this run: **{}**.".format(judge_backend))
    if judge_backend == "stub":
        lines.append(
            "  No live LLM backend was available (no API key / package configured), so the "
            "faithfulness, clarity, and winner values above were derived deterministically "
            "from the rule-based checks, not independently judged. See README for how to "
            "enable the live backend with your own API key."
        )
    lines.append(
        "- `must_not_claim` / `must_include_any` matching is lexical (exact -> ordered "
        "subsequence -> stemmed subsequence), not negation-aware; a hedged or explicitly "
        "negated mention of a banned phrase can register as a match."
    )
    lines.append(
        "- `grounding_score` is a token-overlap-plus-heuristics proxy for faithfulness, not "
        "a semantic entailment check."
    )
    lines.append(
        "- The composite score's weights (`config.yaml: aggregation.weights`) are a stated "
        "modeling choice, not a discovered optimum -- they are designed to be easy to audit "
        "and change, not to be treated as the one correct weighting."
    )
    lines.append(
        "- With only {} queries in this run, the composite margin has limited statistical "
        "power; a small margin should be read as 'no strong signal either way,' not as a "
        "precise ranking.".format(len(queries))
    )
    lines.append("")

    return "\n".join(lines)
