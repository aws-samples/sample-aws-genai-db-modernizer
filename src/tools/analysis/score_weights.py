"""The score-dimension weights an engine's analysis used for one table (#728).

``analysis.json`` records each table's four dimension scores and its confidence, but
not the weights that combined them, and the referee never reads the decision trace
that does (ADR-017 Decision 3). Every engine has one weight set, except OpenSearch,
which weights a search table and a time-series table differently: for it the weights
are the profile whose combination of the table's stored dimensions gives back the
table's stored confidence. Until the engine files carry the weights per profile
(ADR-031 Decision 8, #492), the weights stay in the per-engine catalog modules, and
this is the one place that says which of them an engine uses.
"""

from __future__ import annotations

from collections.abc import Mapping

from src.contracts.analysis_output import ScoreBreakdown
from src.tools.analysis.aurora_common_pattern_catalog import (
    AURORA_MYSQL_SCORE_WEIGHTS,
    AURORA_PG_SCORE_WEIGHTS,
)
from src.tools.analysis.documentdb_pattern_catalog import DOCUMENTDB_SCORE_WEIGHTS
from src.tools.analysis.dynamodb_pattern_catalog import DYNAMODB_SCORE_WEIGHTS
from src.tools.analysis.opensearch_pattern_catalog import (
    OPENSEARCH_SEARCH_WEIGHTS,
    OPENSEARCH_TIMESERIES_WEIGHTS,
)
from src.tools.analysis.scoring import DEFAULT_WEIGHTS, compute_confidence


def weight_profiles(engine: str) -> tuple[Mapping[str, float], ...]:
    """The weight sets ``engine``'s analysis can use, the default one first."""
    profiles: dict[str, tuple[Mapping[str, float], ...]] = {
        "aurora_postgresql": (AURORA_PG_SCORE_WEIGHTS,),
        "aurora_mysql": (AURORA_MYSQL_SCORE_WEIGHTS,),
        "dynamodb": (DYNAMODB_SCORE_WEIGHTS,),
        "documentdb": (DOCUMENTDB_SCORE_WEIGHTS,),
        "opensearch": (OPENSEARCH_SEARCH_WEIGHTS, OPENSEARCH_TIMESERIES_WEIGHTS),
    }
    return profiles.get(engine, (DEFAULT_WEIGHTS,))


def table_weights(engine: str, recommendation: Mapping) -> dict[str, float]:
    """The weights ``engine``'s analysis combined ``recommendation``'s dimensions with."""
    profiles = weight_profiles(engine)
    breakdown = recommendation.get("score_breakdown")
    if breakdown and len(profiles) > 1:
        scores = ScoreBreakdown(**breakdown)
        stored = recommendation.get("confidence_score")
        for weights in profiles:
            if compute_confidence(scores, dict(weights)) == stored:
                return dict(weights)
    return dict(profiles[0])
