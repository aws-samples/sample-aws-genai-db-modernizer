# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
DynamoDB Migration Pattern Catalog

Specialist-curated catalog of migration patterns for DynamoDB.
Each pattern describes a usage pattern that maps well to DynamoDB,
with a specialist-assigned base confidence score.

These scores are expert priors — they get validated and calibrated
through the decision trace feedback loop.

Detection signals are documented as comments above each pattern's
``[engines.dynamodb]`` table. They describe what the detection logic in
dynamodb_analysis_tools.py looks for when matching queries to this pattern.

The entries live in ``src/catalog/patterns/<pattern_type>.toml`` (ADR-031 Decision 8,
#492): ``catalog_patterns``/``catalog_anti_patterns`` build the lists below from them,
under the names the analysis tools import. Edit the TOML file, not this module.
"""

from src.shared.catalog import score_weights
from src.tools.analysis.pattern_catalog_base import (
    CatalogAntiPattern,
    CatalogPattern,
    catalog_anti_patterns,
    catalog_patterns,
)

__all__ = ["CatalogAntiPattern", "CatalogPattern"]


# ==========================================================================
# DynamoDB Pattern Catalog
# ==========================================================================

DYNAMODB_PATTERNS: list[CatalogPattern] = catalog_patterns("dynamodb")

# ==========================================================================
# DynamoDB Anti-Pattern Catalog
# ==========================================================================

DYNAMODB_ANTI_PATTERNS: list[CatalogAntiPattern] = catalog_anti_patterns("dynamodb")


# Lookup helpers
PATTERN_BY_ID: dict[str, CatalogPattern] = {p.pattern_id: p for p in DYNAMODB_PATTERNS}
ANTI_PATTERN_BY_ID: dict[str, CatalogAntiPattern] = {
    ap.pattern_id: ap for ap in DYNAMODB_ANTI_PATTERNS
}


# DynamoDB's score weights: its engine file's [score_weights] (ADR-031 Decision 5, #1002).
# Pattern match weighted highest because catalog scores are specialist-curated and pattern
# fit is the strongest signal for DynamoDB suitability; performance low because DynamoDB's
# value is keeping performance at scale, not improving already-fast queries.
DYNAMODB_SCORE_WEIGHTS: dict[str, float] = score_weights("dynamodb")
