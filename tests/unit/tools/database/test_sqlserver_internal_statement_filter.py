# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Both SQL Server collectors leave SQL Server's own statements out (#1876).

The offline script (``scripts/collect-sqlserver.sql``) and the live collector
(``src/tools/database/sqlserver_tools.py``) drop a cached statement whose batch text
matches one of their ``st.text NOT LIKE`` patterns: the RDS agent (``rdsadmin``),
``msdb``, the DMVs, and since #1876 SQL Server's Query Store / automatic tuning code,
whose batches read the internal ``sys.plan_persist_*`` tables. The two lists are one
list, and each pattern is evaluated here with T-SQL's ``LIKE`` rules (``%``, ``_`` and
``[...]``; the default collation is case-insensitive) against synthetic batch texts.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[4]
SCRIPT = REPO / "scripts" / "collect-sqlserver.sql"
LIVE = REPO / "src" / "tools" / "database" / "sqlserver_tools.py"

_NOT_LIKE_RE = re.compile(r"AND st\.text NOT LIKE '([^']*)'")


def _patterns(path: Path) -> list[str]:
    return _NOT_LIKE_RE.findall(path.read_text(encoding="utf-8"))


def _like(pattern: str) -> re.Pattern[str]:
    """A T-SQL ``LIKE`` pattern as a case-insensitive regex."""
    out, i = [], 0
    while i < len(pattern):
        c = pattern[i]
        if c == "%":
            out.append(".*")
        elif c == "_":
            out.append(".")
        elif c == "[":
            end = pattern.index("]", i)
            out.append("[" + re.escape(pattern[i + 1 : end]) + "]")
            i = end
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("".join(out), re.IGNORECASE | re.DOTALL)


def _excluded(batch: str, path: Path) -> bool:
    return any(_like(p).fullmatch(batch) for p in _patterns(path))


# A Query Store automatic-tuning batch, shaped like the statements the DNN Platform
# collection carried (synthetic text).
INTERNAL_BATCH = (
    "declare @plansByGroup table (plan_id bigint, plan_type nvarchar(10)); "
    "insert into @plansByGroup(plan_id, plan_type) select plan_id, N'?' "
    "from sys.plan_persist_plan where query_id = @queryId; "
    "select p.plan_id from @plansByGroup as p where plan_id = @lastKnownGoodPlanId "
    "and plan_type = @planType"
)

APPLICATION_BATCHES = [
    "CREATE PROCEDURE dbo.GetPlan @planId int AS "
    "SELECT PlanId, PlanName, plan_type FROM dbo.Plans WHERE plan_id = @planId",
    "SELECT * FROM dbo.plan_persist_log WHERE LogId = @id",  # an application table
    "SELECT * FROM dbo.SysPlanPersistence WHERE Id = @id",
    "INSERT INTO @plans (plan_id) SELECT PlanId FROM dbo.Plans WHERE Active = 1",
]


def test_the_two_collectors_share_one_exclusion_list() -> None:
    assert _patterns(SCRIPT) == _patterns(LIVE)
    assert "%sys.plan[_]persist[_]%" in _patterns(SCRIPT)


@pytest.mark.parametrize("path", [SCRIPT, LIVE], ids=["offline", "live"])
def test_the_query_store_internal_batch_is_excluded(path: Path) -> None:
    assert _excluded(INTERNAL_BATCH, path)
    assert _excluded(INTERNAL_BATCH.upper(), path)  # case-insensitive collation


@pytest.mark.passes_on_main  # the negative case: no pattern drops an application batch
@pytest.mark.parametrize("path", [SCRIPT, LIVE], ids=["offline", "live"])
@pytest.mark.parametrize("batch", APPLICATION_BATCHES)
def test_an_application_batch_that_mentions_plan_is_kept(path: Path, batch: str) -> None:
    assert not _excluded(batch, path)


@pytest.mark.parametrize("path", [SCRIPT, LIVE], ids=["offline", "live"])
def test_a_batch_that_also_reads_plan_persist_loses_every_statement(path: Path) -> None:
    """The filter's reach (review of #1879): ``st.text`` is the whole batch or module, so
    an application procedure that also reads ``sys.plan_persist_*`` loses all of its
    statements, the same reach as the existing ``%sys.dm_%`` pattern."""
    mixed = (
        "CREATE PROCEDURE dbo.ReportPlans AS "
        "SELECT PlanId, PlanName FROM dbo.Plans; "
        "SELECT plan_id FROM sys.plan_persist_plan"
    )
    assert _excluded(mixed, path)
    assert _excluded("SELECT * FROM dbo.Users; SELECT * FROM sys.dm_exec_requests", path)
