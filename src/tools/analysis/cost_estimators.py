"""Each engine's analysis cost estimator, by catalog engine id (#1195).

The analysis agents call these on the whole collection, before routing. Synthesis calls
the same ones on the queries the final assignment gives each kept engine
(``src.agents.referee.assigned_cost``), so both figures come from one formula per engine.
Pinned to the catalog's active engines by
``test_engine_catalog.TestEveryKnownEngineHasACatalogFile``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from src.contracts.analysis_output import CostEstimate
from src.tools.analysis.aurora_common_analysis_tools import estimate_aurora_costs
from src.tools.analysis.documentdb_analysis_tools import estimate_documentdb_costs
from src.tools.analysis.dynamodb_analysis_tools import estimate_dynamodb_costs
from src.tools.analysis.opensearch_analysis_tools import estimate_opensearch_costs
from src.tools.analysis.redis_analysis_tools import estimate_redis_costs

# (collector_output, target_region) -> the engine's estimate, as its analysis agent calls it.
CostEstimator = Callable[[dict[str, Any], str], CostEstimate]

COST_ESTIMATORS: dict[str, CostEstimator] = {
    "aurora_mysql": lambda c, r: estimate_aurora_costs(c, r, engine="mysql"),
    "aurora_postgresql": lambda c, r: estimate_aurora_costs(c, r, engine="postgresql"),
    "dynamodb": lambda c, r: estimate_dynamodb_costs(c, r),
    "documentdb": lambda c, r: estimate_documentdb_costs(c, r),
    "opensearch": lambda c, r: estimate_opensearch_costs(c, r),
    "elasticache": lambda c, r: estimate_redis_costs(c, r, None),
}
