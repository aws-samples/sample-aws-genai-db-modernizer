# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The score-dimension weights an engine's analysis used for one table (#728).

``analysis.json`` records each table's four dimension scores and its confidence, but
not the weights that combined them, and the referee never reads the decision trace
that does (ADR-017 Decision 3). The weights are catalog data: each engine file's
``[score_weights.<profile>]`` tables (ADR-031 Decision 5, #1002), read through
``src.shared.catalog.score_weight_profiles``. Every engine has one weight set, except
OpenSearch, which weights a search table and a time-series table differently: for it
the weights are the profile whose combination of the table's stored dimensions gives
back the table's stored confidence.

A saved job's ``analysis.json`` keeps the confidence its analysis computed, with the
weights of the code that wrote it. Routing always applies the catalog's current weights
to the stored dimensions, so a DynamoDB or DocumentDB analysis written before #1002 (cost
at 15%) is routed with today's weights, while its stored ``confidence_score`` still
reflects the old ones. OpenSearch's profiles did not change in #1002, so its profile
match still holds for those files.
"""

from __future__ import annotations

from collections.abc import Mapping

from src.contracts.analysis_output import ScoreBreakdown
from src.shared import catalog
from src.tools.analysis.scoring import compute_confidence


def weight_profiles(engine: str) -> tuple[Mapping[str, float], ...]:
    """The weight sets ``engine``'s analysis can use, the default one first."""
    return catalog.score_weight_profiles(engine)


def table_weights(engine: str, recommendation: Mapping) -> dict[str, float]:
    """The weights routing combines ``recommendation``'s dimensions with: the catalog's
    current ones for ``engine`` (for an analysis written before a weight change, not the
    ones its stored confidence used; see the module docstring)."""
    profiles = weight_profiles(engine)
    breakdown = recommendation.get("score_breakdown")
    if breakdown and len(profiles) > 1:
        scores = ScoreBreakdown(**breakdown)
        stored = recommendation.get("confidence_score")
        for weights in profiles:
            if compute_confidence(scores, dict(weights)) == stored:
                return dict(weights)
    return dict(profiles[0])
