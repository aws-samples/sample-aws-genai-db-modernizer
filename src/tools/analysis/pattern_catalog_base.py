"""
Pattern Catalog Base Types

Shared dataclass definitions used by all engine-specific pattern catalogs.
These define the structure for specialist-curated migration patterns and
anti-patterns that drive the analysis scoring pipeline.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CatalogPattern:
    """A specialist-defined migration pattern."""

    pattern_id: str
    pattern_type: str
    description: str
    base_score: int  # Specialist-assigned confidence (0-100)
    concerns: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CatalogAntiPattern:
    """A specialist-defined anti-pattern (concern, not blocker)."""

    pattern_id: str
    pattern_type: str
    description: str
    severity_weight: float  # 0.0 (negligible) to 1.0 (critical)
    guidance: str = ""
