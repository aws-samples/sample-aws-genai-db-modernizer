"""Canonical, AWS-branded display names for the engines synthesis can target.

Single source of truth for turning an engine id (``aurora_mysql``) into the name a
customer-facing artifact shows (``Aurora MySQL``). Before this module existed, every
consumer kept its own copy, and two of them drifted into non-brand casing:
``src.report.analysis_report.ENGINE_LABELS`` (feeds the decision report's engine
badges and, via JSON injection, the client script in
``src/report/templates/analysis_report.js``) and its hand-kept mirror in
``src/ui/src/utils/ExportReport.js`` both said ``Elasticache``, ``AuroraPostgresql``
and ``AuroraMySQL`` -- no space, wrong internal casing -- which then leaked into the
"Resolved by the assignment" section of the engineering report (#221) once that
section started naming engines (``src.report.renderers``).

``src.report.pptx_report.ENGINE_LABEL`` had the correct names
(``"Aurora MySQL"``, ``"ElastiCache"``, ...) all along, for the six engines it
covers, plus three this module covers that it doesn't (``neptune``, ``keyspaces``,
``aurora``). This module is *not* imported from ``src.report.renderers`` (``pptx_report.py``
imports from ``renderers``, so importing ``pptx_report`` back from a module
``renderers.py`` depends on would be a cycle) -- the dependency runs the other way:
``pptx_report.ENGINE_LABEL`` is now an alias built from ``ENGINE_DISPLAY_NAMES``
here, restricted to the six engines the deck covers, kept under its old name
because other modules and tests still import it directly (#239).
"""

from __future__ import annotations

ENGINE_DISPLAY_NAMES: dict[str, str] = {
    "dynamodb": "DynamoDB",
    "documentdb": "DocumentDB",
    "opensearch": "OpenSearch",
    "elasticache": "ElastiCache",
    "aurora_postgresql": "Aurora PostgreSQL",
    "aurora_mysql": "Aurora MySQL",
    "neptune": "Neptune",
    "keyspaces": "Keyspaces",
    "aurora": "Aurora",
}


def display_engine(engine: str) -> str:
    """Display name for ``engine``, or a title-cased fallback if it is not known.

    The fallback only prettifies the id (``unmapped_engine`` -> ``Unmapped Engine``)
    rather than raising, so an engine added to a contract but not yet to
    ``ENGINE_DISPLAY_NAMES`` still renders as readable prose instead of breaking the
    report.
    """
    if engine in ENGINE_DISPLAY_NAMES:
        return ENGINE_DISPLAY_NAMES[engine]
    return engine.replace("_", " ").title() if engine else engine


# The collector's raw source-engine key (``source_database_engine``), lower-cased --
# distinct from ENGINE_DISPLAY_NAMES, which only names synthesis's target engines.
# A plain ``display_engine("mysql")`` fallback would title-case to "Mysql", so these
# get their own, correctly-cased names (#225).
SOURCE_ENGINE_DISPLAY_NAMES: dict[str, str] = {
    "mysql": "MySQL",
    "mariadb": "MariaDB",
    "postgresql": "PostgreSQL",
    "postgres": "PostgreSQL",
}


def display_source_database(engine: str) -> str:
    """ "the source MySQL database" phrasing for a raw source-engine key.

    Used wherever a migration wave's ``moves_from`` is rendered: data always
    moves from the current source database, never from the end-state Aurora
    engine it has not reached yet (#225).
    """
    name = SOURCE_ENGINE_DISPLAY_NAMES.get(engine, display_engine(engine))
    return f"the source {name} database"
