# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#1938: the routine-migration artifact as report facts (ADR-032, ADR-034 Decision 7).

``src.shared.routine_facts.routine_facts`` reads one ``RoutineMigrationContract`` and
computes, once, the headline counts every deliverable prints and one row per routine
behind a routine risk, with its class, its severity and what set it. The facts carry
ids, kinds, rule ids, reason codes and counts only: no routine text and nothing a check
found in it.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from src.contracts.routine_migration import RoutineMigrationContract
from src.contracts.synthesis_output import RoutineFacts
from src.shared.routine_facts import (
    PROOF_LABEL,
    ROUTINE_RISK_SEVERITY,
    read_routine_facts,
    routine_facts,
)
from src.storage.local_store import LocalArtifactStore
from src.storage.routine_migration_store import routine_migration_path
from tests.unit.agents.referee.test_routing_never_reads_gaps import (
    GRAPH,
    ROUTING_MODULES,
    _chain,
    _reachable,
)

# A finding the artifact may hold (what a check found): never in the facts.
FINDING = "zz_finding_marker_1938"

RULES: dict[str, dict[str, Any]] = {
    "pg-language": {"rule": "check", "impact": "blocks_move"},
    "pg-removed-setting": {"rule": "check", "impact": "needs_rewrite"},
    "pg-public-schema": {"rule": "check", "impact": "behaves_differently"},
    "pg-dependency": {"rule": "check", "impact": "blocks_move"},
    "pg-superuser": {"rule": "check", "impact": "needs_rewrite"},
    "pg-owner-to-target-user": {"rule": "rewrite", "impact": None},
}


def _row(rid: str, status: str = "as_is", kind: str = "function", **over: Any) -> dict:
    row = {
        "routine_id": rid,
        "kind": kind,
        "schema_name": "public",
        "name": rid.split(".")[-1].split("(")[0],
        "specific_name": rid,
        "source_text": "exact",
        "body_status": "collected",
        "status": status,
        "reasons": [],
        "blocked_by": [],
        "deploy_order": 1,
        "dependencies": [],
        "dependencies_complete": True,
        "rewrites": [],
        "notes": [],
        "checks": [],
        "rules_passed": 3,
    }
    row.update(over)
    return row


def _fail(rule_id: str) -> dict:
    return {"rule_id": rule_id, "outcome": "fail", "findings": [FINDING]}


def _unknown(rule_id: str) -> dict:
    return {"rule_id": rule_id, "outcome": "unknown", "findings": [FINDING]}


REWRITE = {"rule_id": "pg-owner-to-target-user", "rewrite": "owner"}
ORDERS = {"kind": "table", "name": "public.orders", "found": True, "routines": []}


def _artifact(
    *rows: dict, source: str = "postgresql", target: str = "aurora_postgresql"
) -> RoutineMigrationContract:
    counts = {s: 0 for s in ("as_is", "rewritten", "model_repaired", "needs_attention")}
    ordered = []
    for i, row in enumerate(rows, start=1):
        counts[row["status"]] += 1
        ordered.append({**row, "deploy_order": i})
    return RoutineMigrationContract.model_validate(
        {
            "job_id": "j1",
            "database_name": "shop",
            "assignment_version": 1,
            "source_engine": source,
            "target_engine": target,
            "target_user": "postgres" if source == "postgresql" else "admin",
            "routine_sources": True,
            "total": len(rows),
            "counts": counts,
            "rules": [
                {"rule_id": k, "reason": f"{k} reason.", "fix_hint": f"{k} fix.", **v}
                for k, v in RULES.items()
            ],
            "routines": ordered,
        }
    )


MIXED = _artifact(
    _row("public.plain()"),
    _row("public.plain_unknown()", checks=[_unknown("pg-dependency")]),
    _row(
        "public.owned()",
        "rewritten",
        rewrites=[REWRITE],
        notes=["runs_as_target_user"],
        checks=[_unknown("pg-superuser")],
    ),
    _row(
        "public.py_fn()",
        "needs_attention",
        reasons=["pg-language"],
        checks=[_fail("pg-language")],
        dependencies=[ORDERS],
    ),
    _row(
        "public.f_a()",
        "needs_attention",
        reasons=["dependency_cycle", "depends_on_needs_attention"],
        blocked_by=[{"kind": "function", "routine_id": "public.f_b()"}],
    ),
    _row(
        "public.f_b()",
        "needs_attention",
        reasons=["dependency_cycle", "depends_on_needs_attention"],
        blocked_by=[{"kind": "function", "routine_id": "public.f_a()"}],
    ),
    _row(
        "public.public_path()",
        "needs_attention",
        reasons=["pg-public-schema"],
        checks=[_fail("pg-public-schema")],
    ),
    _row(
        "public.secret()",
        "needs_attention",
        source_text="not_collected",
        body_status="not_readable",
        reasons=["source_text_not_collected"],
    ),
    _row(
        "public.opted_out()",
        "needs_attention",
        source_text="not_collected",
        body_status="opted_out",
        reasons=["source_text_not_collected"],
    ),
    _row(
        "public.orders.tg_audit",
        "needs_attention",
        kind="trigger",
        source_text="not_exact",
        reasons=["source_text_not_exact"],
        dependencies=[ORDERS],
    ),
)


def _by_id(facts: dict) -> dict[str, dict]:
    return {r["routine_id"]: r for r in facts["routines"]}


def test_each_routine_is_in_one_headline_count_and_the_counts_split_the_total() -> None:
    facts = routine_facts(MIXED, 1)
    assert facts is not None
    assert {k: v for k, v in facts.items() if k.endswith("_count")} == {
        "routine_count": 10,
        "carried_count": 3,
        "as_is_count": 2,
        "rewritten_count": 1,
        "repaired_count": 0,
        "needs_repair_count": 4,
        "not_exact_count": 1,
        "not_checked_count": 0,
        "opted_out_count": 1,
        "not_readable_count": 1,
        "unknown_checks_count": 2,
        "rewritten_carried_count": 1,
        "migrate_yourself_count": 5,
        "behaves_differently_count": 1,
        "carried_review_count": 2,
    }
    assert facts["counts_by_kind"] == {"function": 9, "trigger": 1}
    assert facts["reason_counts"] == {
        "dependency_cycle": 2,
        "depends_on_needs_attention": 2,
        "pg-language": 1,
        "pg-public-schema": 1,
        "source_text_not_collected": 2,
        "source_text_not_exact": 1,
    }
    assert facts["proof"] == PROOF_LABEL == "checked, not load-tested"
    RoutineFacts.model_validate(facts)


def test_the_headline_line_counts_and_names_no_routine_or_table() -> None:
    facts = routine_facts(MIXED, 1)
    assert facts is not None
    assert facts["line"] == (
        "3 of 10 routines carry over: 2 as they are, 1 rewritten. 7 were not carried "
        "automatically: 4 need repair (1 of them behaves differently), 1 was rebuilt around "
        "a literal, 1 has no code collected, 1 was not readable. For 2 carried routines not "
        "every check could run; the other 1 is checked, not load-tested."
    )
    for row in MIXED.routines:
        assert row.name not in facts["line"]
    assert "orders" not in facts["line"]


def test_the_to_do_is_worded_once_in_the_facts_with_its_wave() -> None:
    """Maintainer decision on #1938: a to-do for the customer, never "blocked"."""
    waves = [{"wave": 1, "engines": ["aurora_postgresql"]}, {"wave": 2, "engines": ["dynamodb"]}]
    facts = routine_facts(MIXED, 1, waves) or {}
    assert facts["wave"] == 1
    assert facts["migrate_yourself_line"] == (
        "we could not translate 5 routines automatically; migrate them yourself before wave 1"
    )
    assert facts["migrate_yourself_step"] == (
        "migrate these routines yourself before wave 1, then check them again"
    )
    one = _artifact(
        _row("public.x()", "needs_attention", reasons=["pg-language"], checks=[_fail("pg-language")])
    )  # fmt: skip
    facts = routine_facts(one, 1) or {}
    assert facts["wave"] is None
    # No wave brings in the target: no timing advice, only the cutover sentence.
    assert facts["migrate_yourself_line"] == (
        "we could not translate 1 routine automatically; migrate it yourself; the cutover "
        "is your responsibility"
    )
    assert facts["migrate_yourself_step"] == (
        "migrate this routine yourself, then check it again; the cutover is your " "responsibility"
    )
    assert "before cutover" not in json.dumps(facts)
    clean = routine_facts(_artifact(_row("public.plain()")), 1) or {}
    assert (clean["migrate_yourself_line"], clean["migrate_yourself_step"]) == (None, None)
    for text in (json.dumps(routine_facts(MIXED, 1, waves)),):
        assert "block" not in text.replace("blocked_by", "").replace("blocks_move", "")


def test_a_routine_the_pipeline_could_not_translate_is_high_and_says_why() -> None:
    rows = _by_id(routine_facts(MIXED, 1) or {})
    py = rows["public.py_fn()"]
    assert (py["category"], py["risk_classes"], py["severity"]) == (
        "needs_repair",
        ["migrate_yourself"],
        "HIGH",
    )
    assert py["severity_basis"] == ["pg-language"]
    assert py["tables"] == ["public.orders"]
    cycle = rows["public.f_a()"]
    assert cycle["risk_classes"] == ["migrate_yourself"]
    assert cycle["severity_basis"] == ["dependency_cycle", "depends_on_needs_attention"]
    assert cycle["blocked_by"] == [{"kind": "function", "routine_id": "public.f_b()"}]


def test_a_routine_whose_only_failed_check_behaves_differently_is_medium() -> None:
    row = _by_id(routine_facts(MIXED, 1) or {})["public.public_path()"]
    assert (row["risk_classes"], row["severity"], row["severity_basis"]) == (
        ["behaves_differently"],
        "MEDIUM",
        ["pg-public-schema"],
    )
    # With a failed rewrite-needing check too, the customer migrates it.
    both = _artifact(
        _row(
            "public.x()",
            "needs_attention",
            reasons=["pg-removed-setting", "pg-public-schema"],
            checks=[_fail("pg-removed-setting"), _fail("pg-public-schema")],
        )
    )
    row = _by_id(routine_facts(both, 1) or {})["public.x()"]
    assert (row["risk_classes"], row["severity_basis"]) == (
        ["migrate_yourself"],
        ["pg-removed-setting"],
    )


def test_unreadable_or_inexact_code_is_high_and_opted_out_code_is_medium() -> None:
    rows = _by_id(routine_facts(MIXED, 1) or {})
    for rid, category in (
        ("public.secret()", "not_readable"),
        ("public.orders.tg_audit", "not_exact"),
    ):
        assert rows[rid]["category"] == category
        assert rows[rid]["risk_classes"] == ["migrate_yourself"]
        assert rows[rid]["severity"] == "HIGH"
    opted = rows["public.opted_out()"]
    assert (opted["category"], opted["risk_classes"], opted["severity"]) == (
        "opted_out",
        ["opted_out"],
        ROUTINE_RISK_SEVERITY["opted_out"],
    )
    assert ROUTINE_RISK_SEVERITY["opted_out"] == "MEDIUM"
    # No routine sources at all (not opted out): not checked, and the customer's to-do.
    none = _artifact(
        _row("public.y()", "needs_attention", source_text="not_collected",
             reasons=["source_text_not_collected"])
    )  # fmt: skip
    row = _by_id(routine_facts(none, 1) or {})["public.y()"]
    assert (row["category"], row["risk_classes"]) == ("not_checked", ["migrate_yourself"])


def test_rewritten_and_unknown_checks_roll_up_low_and_a_clean_routine_has_no_row() -> None:
    rows = _by_id(routine_facts(MIXED, 1) or {})
    assert "public.plain()" not in rows
    owned = rows["public.owned()"]
    assert owned["risk_classes"] == ["carried_review"]
    assert (owned["severity"], owned["severity_basis"]) == (
        "LOW",
        ["pg-owner-to-target-user", "pg-superuser"],
    )
    assert owned["unknown_checks"] == ["pg-superuser"]
    assert owned["notes"] == ["runs_as_target_user"]
    unknown = rows["public.plain_unknown()"]
    assert (unknown["risk_classes"], unknown["severity_basis"]) == (
        ["carried_review"],
        ["pg-dependency"],
    )


def test_the_rows_are_in_deploy_order_and_name_their_rules() -> None:
    facts = routine_facts(MIXED, 1) or {}
    orders = [r["deploy_order"] for r in facts["routines"]]
    assert orders == sorted(orders)
    named = {r["rule_id"] for r in facts["rules"]}
    assert named == {
        "pg-language",
        "pg-public-schema",
        "pg-dependency",
        "pg-superuser",
        "pg-owner-to-target-user",
    }


def test_no_finding_or_routine_text_reaches_the_facts() -> None:
    text = json.dumps(routine_facts(MIXED, 1))
    assert FINDING not in text
    assert "findings" not in text and "CREATE" not in text


def test_the_severity_rules_never_depend_on_the_engine() -> None:
    """The same rows on a PostgreSQL and a MySQL artifact get the same classes."""
    rows = [r.model_dump(mode="json") for r in MIXED.routines]
    for row in rows:
        row.pop("deploy_order")
    pg = routine_facts(_artifact(*rows), 1) or {}
    mysql = routine_facts(_artifact(*rows, source="mysql", target="aurora_mysql"), 1) or {}
    strip = ("source_engine", "target_engine")
    assert {k: v for k, v in pg.items() if k not in strip} == {
        k: v for k, v in mysql.items() if k not in strip
    }


def test_no_artifact_gives_no_facts(tmp_path) -> None:
    assert routine_facts(None, 1) is None
    store = LocalArtifactStore(base_dir=str(tmp_path))
    assert read_routine_facts(store, "shop", "j1", 3) is None
    assert read_routine_facts(store, "shop", "j1", 0) is None


def test_the_facts_read_the_newest_artifact_at_or_below_the_version(tmp_path) -> None:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json(routine_migration_path("shop", "j1", 1), MIXED.model_dump(mode="json"))
    facts = read_routine_facts(store, "shop", "j1", 3)
    assert facts is not None and facts["assignment_version"] == 1
    assert facts == routine_facts(MIXED, 1)


def test_an_empty_artifact_has_a_line_and_no_rows() -> None:
    facts = routine_facts(_artifact(_row("public.plain()")), 1) or {}
    assert facts["routines"] == [] and facts["carried_count"] == 1
    assert facts["line"] == "1 of 1 routine carries over: 1 as it is. Checked, not load-tested."


@pytest.mark.parametrize("module", ROUTING_MODULES)
def test_no_routing_module_reaches_the_routine_facts(module: str) -> None:
    """ADR-034 Decision 8: routines never change routing."""
    seen = _reachable(module, GRAPH)
    for target in ("src.shared.routine_facts", "src.storage.routine_migration_store"):
        assert target not in seen, _chain(seen, target)


def test_synthesis_reads_the_routine_facts() -> None:
    seen = _reachable("src.agents.referee.synthesis_handler", GRAPH)
    assert "src.shared.routine_facts" in seen


def test_tables_leaving_aurora_never_become_a_risk() -> None:
    """Step 1 mirrors every table, so a table the end state moves off Aurora is
    information on the artifact's row, never a risk class."""
    leaving = [{"table": "public.orders", "engines": ["dynamodb"], "source_of_truth": "dynamodb"}]
    facts = routine_facts(_artifact(_row("public.plain()", tables_leaving_aurora=leaving)), 1)
    assert facts is not None and facts["routines"] == []
    assert "tables_leaving_aurora" not in json.dumps(facts)
    from src.shared.migration_risk_rules import routine_risks

    assert routine_risks(facts) == []
    # On a routine that is in a risk, it changes nothing either.
    rows = [r.model_dump(mode="json") for r in MIXED.routines]
    with_leaving = [{**r, "tables_leaving_aurora": leaving} for r in rows]
    for r in (*rows, *with_leaving):
        r.pop("deploy_order")
    assert routine_facts(_artifact(*with_leaving), 1) == routine_facts(_artifact(*rows), 1)
