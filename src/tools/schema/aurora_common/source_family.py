"""Classify the source database engine and pick a migration strategy.

The collector reports the source engine in its metadata. We only need to know
which *family* it belongs to, because that decides whether a target design is a
homogeneous carry-over or a heterogeneous translation.
"""

from __future__ import annotations

from typing import Literal

SourceFamily = Literal["postgresql", "mysql", "other"]
MigrationStrategy = Literal["carry_over", "translate"]
AuroraTarget = Literal["aurora_postgresql", "aurora_mysql"]

_PG_ALIASES = {"postgresql", "postgres", "pg", "aurora_postgresql", "aurora-postgresql"}
_MYSQL_ALIASES = {"mysql", "mariadb", "aurora_mysql", "aurora-mysql"}


def classify_source_family(source_engine: str) -> SourceFamily:
    """Map a raw source-engine string to postgresql | mysql | other."""
    engine = (source_engine or "").strip().lower()
    if engine in _PG_ALIASES:
        return "postgresql"
    if engine in _MYSQL_ALIASES:
        return "mysql"
    return "other"


def migration_strategy(source_engine: str, target: AuroraTarget) -> MigrationStrategy:
    """carry_over when source shares the target's family, else translate.

    Rule (ADR-028): PG->Aurora-PG and MySQL->Aurora-MySQL are carry_over;
    every other source translates.
    """
    family = classify_source_family(source_engine)
    if target == "aurora_postgresql":
        return "carry_over" if family == "postgresql" else "translate"
    return "carry_over" if family == "mysql" else "translate"
