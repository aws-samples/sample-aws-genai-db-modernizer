"""Canonical, AWS-branded display names for the engines synthesis can target.

Single source of truth for turning an engine id (``aurora_mysql``) into the name a
customer-facing artifact shows (``Aurora MySQL``). Before this module existed, every
consumer kept its own copy, and two of them drifted into non-brand casing: the
interactive HTML report's engine label table and its hand-kept JS mirror (both
retired in #987) said ``Elasticache``, ``AuroraPostgresql`` and ``AuroraMySQL`` --
no space, wrong internal casing -- which then leaked into the
"Resolved by the assignment" section of the engineering report (#221) once that
section started naming engines (``src.report.engineering_report``).

The old per-renderer engine label table had the correct names
(``"Aurora MySQL"``, ``"ElastiCache"``, ...) all along, for the six engines it
covered, plus three this module covers that it didn't (``neptune``, ``keyspaces``,
``aurora``). This module is the single source: every renderer reads it, and it is
*not* imported from ``src.report.renderers``, which would make a cycle (#239).
"""

from __future__ import annotations

import re

from src.shared import catalog

# Built from the engine catalog (``src/catalog/engines/*.toml``, ADR-031 Decision 8, #492),
# in its ``display_order``; the UI's copy is generated from the same files.
ENGINE_DISPLAY_NAMES: dict[str, str] = {e.id: e.display_name for e in catalog.engines()}


# A raw engine id in text, longest id first so "aurora_mysql" is matched whole, not as
# "aurora" plus a dangling "_mysql" (re's alternation takes the first branch that matches
# at a given position, not the longest). The customer-text guard finds leaks with it and
# ``display_engine_ids`` rewrites them, so the two always agree on what an id is (#1574).
ENGINE_ID_RE = re.compile(
    r"\b("
    + "|".join(re.escape(k) for k in sorted(ENGINE_DISPLAY_NAMES, key=len, reverse=True))
    + r")\b"
)
# An engine id that is also an AWS IAM service prefix names an IAM action or condition
# key when a colon and an identifier follow it ("dynamodb:Attributes",
# "dynamodb:LeadingKeys", "elasticache:CreateCacheCluster", an ARN's
# "arn:aws:dynamodb:us-east-1"): AWS's own spelling, not a raw engine id in prose
# (#1574). Only these two ids are IAM prefixes; "dynamodb: the table" (a colon and a
# space) and every other id are still raw ids.
IAM_SERVICE_PREFIXES = frozenset({"dynamodb", "elasticache"})
_IAM_NAME_AFTER = re.compile(r":[A-Za-z*]")
_CODE_SPAN = re.compile(r"`[^`\n]*`")
# Part of a path, a file name or an identifier: a "/", "\\", "-", "." or "$" right before
# the id, or a "/", "\\" or "." followed by a name character right after it.
_PATH_BEFORE = re.compile(r"[/\\.$-]$")
_PATH_AFTER = re.compile(r"[/\\]|\.[A-Za-z0-9_]")


def is_iam_name(text: str, match: re.Match[str]) -> bool:
    """Whether the engine id ``match`` found is an IAM action, condition key or ARN part."""
    return match.group(0) in IAM_SERVICE_PREFIXES and bool(_IAM_NAME_AFTER.match(text, match.end()))


def display_engine_ids(text: str) -> str:
    """``text`` with each raw engine id in its prose written as the engine's display name
    ("stays on aurora_postgresql" -> "stays on Aurora PostgreSQL", #1574).

    A model writes the ids it was given; the deliverables name engines. Word-boundary
    matches only, and never inside a code span, in an IAM action or condition key
    (``dynamodb:Attributes``), an ARN, a path or a file name (``analysis-dynamodb/``).
    """
    if not text or not ENGINE_ID_RE.search(text):
        return text
    spans = [(m.start(), m.end()) for m in _CODE_SPAN.finditer(text)]

    def name(m: re.Match[str]) -> str:
        if any(start <= m.start() < end for start, end in spans) or is_iam_name(text, m):
            return m.group(0)
        if _PATH_BEFORE.search(text[: m.start()]) or _PATH_AFTER.match(text, m.end()):
            return m.group(0)
        return ENGINE_DISPLAY_NAMES[m.group(0)]

    return ENGINE_ID_RE.sub(name, text)


# ADR-031 Decision 9: engines that exist in the catalog only as future placeholders
# (``status = "future"``). They are named so contracts, the UI and reports can talk about
# them, but they are never a routing candidate: not ownable, not scored, not a
# reality-check absorber, whatever an analysis output says. Every routing entry point
# drops them via ``without_placeholder_engines``.
PLACEHOLDER_ENGINES: frozenset[str] = catalog.engine_ids(status="future")


def without_placeholder_engines(analysis_outputs: dict[str, dict]) -> dict[str, dict]:
    """Copy of ``analysis_outputs`` without placeholder engines (ADR-031 Decision 9)."""
    return {e: a for e, a in analysis_outputs.items() if e not in PLACEHOLDER_ENGINES}


# A table's writer when no engine in the customer's engine scope can serve any of its
# writes (#1329, ADR-031 Amendment 15): the source database keeps them, and each
# engine that reads the table holds a replica kept in sync from it. Not an engine:
# never assigned, never in the catalog, only a ``source_of_truth`` value.
SOURCE_DATABASE = "source_database"
SOURCE_DATABASE_NAME = "Source database"


def display_engine(engine: str) -> str:
    """Display name for ``engine``, or a title-cased fallback if it is not known.

    The fallback only prettifies the id (``unmapped_engine`` -> ``Unmapped Engine``)
    rather than raising, so an engine added to a contract but not yet to
    ``ENGINE_DISPLAY_NAMES`` still renders as readable prose instead of breaking the
    report.
    """
    if engine in ENGINE_DISPLAY_NAMES:
        return ENGINE_DISPLAY_NAMES[engine]
    if engine == SOURCE_DATABASE:
        return SOURCE_DATABASE_NAME
    return engine.replace("_", " ").title() if engine else engine


_ID_BY_NAME = {name.casefold(): eid for eid, name in ENGINE_DISPLAY_NAMES.items()}


def canonical_engine_id(engine: str) -> str:
    """``engine`` as a catalog engine id, from a variant a model or an older
    artifact wrote: ``"aurora-mysql"``, ``"Aurora MySQL"`` and ``" AURORA_MYSQL "``
    all give ``"aurora_mysql"``. An id the catalog does not know comes back as given
    (stripped), never guessed (#950)."""
    raw = str(engine or "").strip()
    key = raw.casefold().replace("-", "_").replace(" ", "_")
    if key in ENGINE_DISPLAY_NAMES:
        return key
    return _ID_BY_NAME.get(raw.casefold(), raw)


# The collector's raw source-engine key (``source_database_engine``), lower-cased --
# distinct from ENGINE_DISPLAY_NAMES, which only names synthesis's target engines.
# A plain ``display_engine("mysql")`` fallback would title-case to "Mysql", so these
# get their own, correctly-cased names (#225).
SOURCE_ENGINE_DISPLAY_NAMES: dict[str, str] = {
    "mysql": "MySQL",
    "mariadb": "MariaDB",
    "postgresql": "PostgreSQL",
    "postgres": "PostgreSQL",
    "sqlserver": "SQL Server",
    "oracle": "Oracle",
    "db2": "Db2",
}


def display_source_database(engine: str) -> str:
    """ "the source MySQL database" phrasing for a raw source-engine key.

    Used wherever a migration wave's ``moves_from`` is rendered: data always
    moves from the current source database, never from the end-state Aurora
    engine it has not reached yet (#225).
    """
    name = SOURCE_ENGINE_DISPLAY_NAMES.get(engine, display_engine(engine))
    return f"the source {name} database"


def heterogeneous_source_clause(source_engine: str, version: str = "") -> str:
    """The shared opening of the heterogeneous-source sentence (#381, #743).

    A source with no Aurora engine of its own dialect (SQL Server, Oracle, Db2)
    sends both Aurora engines to compete on the collected workload. The wave
    rationale and the executive summary each finish the sentence with their own
    tail (who selected the engine, and the trace), so they cannot word this part
    differently. The wave rationale also passes the collected ``version``
    ("The source SQL Server database (15.0.2000.5) has ..."); the summary has none.
    """
    source = display_source_database(source_engine)
    detail = f" ({version})" if version else ""
    return (
        f"{source[:1].upper()}{source[1:]}{detail} has no Aurora engine of its own dialect: "
        "Aurora MySQL and Aurora PostgreSQL both competed on the collected workload"
    )


def scoped_source_clause(source_engine: str, reason: str, version: str = "") -> str:
    """The opening of the sentence for an Aurora engine the customer's engine scope
    chose (#1327, ADR-031 Decision 2 Amendment 14), in place of
    :func:`heterogeneous_source_clause`: nothing competed, the scope decided.

    ``reason`` is ``AuroraEngineChoice.reason`` ("The customer's engine scope leaves
    out Aurora MySQL, ..."); ``version`` as for the heterogeneous clause.
    """
    source = display_source_database(source_engine)
    detail = f" ({version})" if version else ""
    said = reason.strip().rstrip(".")
    said = said[:1].lower() + said[1:] if said[:4] == "The " else said
    return f"For {source}{detail}, {said}"
