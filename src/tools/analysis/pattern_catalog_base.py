"""
Pattern Catalog Base Types

Shared dataclass definitions used by all engine-specific pattern catalogs.
These define the structure for specialist-curated migration patterns and
anti-patterns that drive the analysis scoring pipeline.

The entries themselves live in ``src/catalog/patterns/<pattern_type>.toml`` (ADR-031
Decision 8, #492); ``catalog_patterns`` and ``catalog_anti_patterns`` build each
engine catalog's list from those files.
"""

from dataclasses import dataclass, field

from src.shared.catalog.patterns import CatalogName, catalog_entries


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


def catalog_patterns(catalog: CatalogName) -> list[CatalogPattern]:
    """One engine catalog's fit patterns, built from ``src/catalog/patterns/*.toml``.

    The order is ``catalog_id`` order, which the analysis tools index into.
    """
    return [
        CatalogPattern(
            pattern_id=support.catalog_id or "",
            pattern_type=pattern_type,
            description=support.why,
            base_score=support.score or 0,
            concerns=list(support.concerns),
        )
        for pattern_type, support in catalog_entries(catalog, "fit")
    ]


def catalog_anti_patterns(catalog: CatalogName) -> list[CatalogAntiPattern]:
    """One engine catalog's anti-patterns, built from ``src/catalog/patterns/*.toml``.

    ``description`` is the entry's ``why`` followed by its ``engine_hint`` sentence, when
    it has one.
    """
    return [
        CatalogAntiPattern(
            pattern_id=support.catalog_id or "",
            pattern_type=pattern_type,
            description=(
                f"{support.why} {support.engine_hint}" if support.engine_hint else support.why
            ),
            severity_weight=support.severity or 0.0,
            guidance=support.guidance,
        )
        for pattern_type, support in catalog_entries(catalog, "anti_pattern")
    ]
