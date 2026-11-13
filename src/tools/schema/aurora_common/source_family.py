# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

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


TypeFamily = Literal["postgresql", "mysql", "oracle", "sqlserver", "other"]
_ORACLE_ALIASES = {"oracle"}
_SQLSERVER_ALIASES = {"sqlserver", "sql_server", "sql server", "mssql"}


def classify_type_family(source_engine: str) -> TypeFamily:
    """``classify_source_family``, with Oracle and SQL Server named: the sources whose
    native ``data_type`` the draft DDL maps (#274, #2219). The migration strategy and
    the draft's ``source_family`` stay on the three-way classification."""
    family = classify_source_family(source_engine)
    if family != "other":
        return family
    engine = (source_engine or "").strip().lower()
    if engine in _ORACLE_ALIASES:
        return "oracle"
    if engine in _SQLSERVER_ALIASES:
        return "sqlserver"
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
