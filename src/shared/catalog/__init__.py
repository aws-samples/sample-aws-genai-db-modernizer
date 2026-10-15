"""The rule catalog (ADR-031 Decision 8). This slice carries the engine files only."""

from src.shared.catalog.engines import (
    ACTIVE,
    CATALOG_DIR,
    REPORTABLE,
    CatalogError,
    EngineColors,
    EngineEntry,
    engine_ids,
    engines,
    load_engine_catalog,
)

__all__ = [
    "ACTIVE",
    "CATALOG_DIR",
    "REPORTABLE",
    "CatalogError",
    "EngineColors",
    "EngineEntry",
    "engine_ids",
    "engines",
    "load_engine_catalog",
]
