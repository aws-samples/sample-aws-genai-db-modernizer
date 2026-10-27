"""Concurrent group schema designs each read only their own input (#1054).

``run_schema_design_auto`` designs a split engine's groups in parallel threads.
The designer modules used to keep the group's input paths in module globals
that the designer's ``load_agent_input`` tool read later, so with more than one
group in flight a designer read the input of whichever group set them last
(on wordpress at concurrency 3, two of the three DynamoDB drafts held another
group's queries and none of their own).

This runs the real grouped DynamoDB path on the deterministic wordpress run (39
DynamoDB queries, 3 groups) at concurrency 3 with the model
calls replayed. The stand-in designer calls the input tool it was given, as a model
does, after a barrier that holds every group's designer until all three are in
flight, then designs one access pattern per input entry the tool returned. Each group's draft must
hold exactly its own group's queries. No test calls a model.
"""

from __future__ import annotations

import copy
import json
import os
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from src.agents.referee.assignment_overrides import mark_assignment_customer_approved
from src.agents.schema_design.handler import run_schema_design_auto
from src.contracts.dynamodb_model_output import DynamoDBModelOutputContract
from src.contracts.dynamodb_pe_review import PEReviewResult, ReviewVerdict
from src.storage.local_store import LocalArtifactStore
from tests.e2e.pipeline import REPO, run_pipeline
from tests.shared_runs import cached_run, copy_run

DB, JOB = "wordpress", "job-concurrent-groups"
# 4 since #992: a table's writes land on one engine, so wp_postmeta's three UPDATEs
# and wp_woocommerce_order_itemmeta's UPDATE join their tables' other writes on
# DynamoDB (35 -> 39 queries), and the splitter makes a fourth group. 3 since #1602:
# the splitter caps a group at 20 access patterns, not 20 statements.
GROUPS = 3
TEMPLATE = json.loads(
    (
        REPO / "docs/examples/wordpress/load-test-results/schema-dynamodb/v2/schema_output.json"
    ).read_text()
)


def _design_from(queries: list[dict]) -> DynamoDBModelOutputContract:
    design = copy.deepcopy(TEMPLATE)
    pattern = design["access_patterns"][0]
    table = design["table_definitions"][0]["table_name"]
    design["access_patterns"] = [
        {
            **pattern,
            "pattern_id": f"DDB-AP-{i + 1}",
            "query_ids": [q["query_id"]],
            "source_tables": list(q.get("tables_accessed") or pattern.get("source_tables") or []),
            "table_name": table,
        }
        for i, q in enumerate(queries)
    ]
    return DynamoDBModelOutputContract.model_validate(design)


# What each group's designer read through its tool, and what the runner's
# numbers check was given, by thread: the two must be the same group's.
read_metrics: dict[int, Any] = {}
checked_metrics: dict[int, Any] = {}


def _replay_agent(barrier: threading.Barrier) -> type:
    started: set[int] = set()

    class _ReplayAgent:
        """Stands in for ``strands.Agent`` in the DynamoDB designer: no model call."""

        def __init__(self, *, structured_output_model: type, tools: Any = (), **_: Any) -> None:
            self.kind = structured_output_model
            self.tools = list(tools or [])

        def __call__(self, prompt: str) -> SimpleNamespace:
            if self.kind is PEReviewResult:
                return SimpleNamespace(
                    structured_output=PEReviewResult(
                        verdict=ReviewVerdict.APPROVED, summary="Approved."
                    )
                )
            # Every group's designer is in flight before any reads its input
            # (its first call only; the numbers check's correction turns follow).
            if threading.get_ident() not in started:
                started.add(threading.get_ident())
                barrier.wait(timeout=30)
            [load_input] = [t for t in self.tools if t.__name__ == "load_agent_input"]
            projected = load_input()
            read_metrics[threading.get_ident()] = projected["design_metrics"]
            return SimpleNamespace(
                structured_output=_design_from(projected["collector"]["queries"]["query_patterns"])
            )

    return _ReplayAgent


def _record_check(engine: str, design: dict, metrics: Any) -> list[str]:
    """Stands in for the numbers check: records the metrics it was given."""
    checked_metrics[threading.get_ident()] = metrics
    return []


@pytest.fixture(scope="module")
def drafts(tmp_path_factory) -> dict[int, tuple[set[str], set[str]]]:
    cached = cached_run(
        tmp_path_factory,
        f"pipeline-wordpress-{JOB}",
        lambda d: run_pipeline("wordpress", d / "artifacts", JOB),
    )
    root = copy_run(cached, tmp_path_factory.mktemp("groups") / "run") / "artifacts"
    store = LocalArtifactStore(base_dir=str(root))
    mark_assignment_customer_approved(store, DB, JOB)
    barrier = threading.Barrier(GROUPS)
    with (
        patch("src.tools.schema.dynamodb_schema_agent.Agent", _replay_agent(barrier)),
        patch("src.tools.schema.dynamodb_schema_agent._build_model", return_value=object()),
        patch(
            "src.tools.schema.dynamodb_schema_agent.check_design_numbers",
            side_effect=_record_check,
        ),
        patch.dict(os.environ, {"SCHEMA_GROUP_CONCURRENCY": str(GROUPS)}),
    ):
        run_schema_design_auto(JOB, DB, "dynamodb", store, 2, llm_mode="bedrock")
    base = f"{DB}/{JOB}/schema-dynamodb/v2"
    manifest = store.read_json(f"{base}/groups_manifest.json")
    out: dict[int, tuple[set[str], set[str]]] = {}
    for group in manifest["groups"]:
        idx = group["group_index"]
        own = {
            q["query_id"]
            for q in store.read_json(f"{base}/input_group_{idx}.json")["collector_output"][
                "queries"
            ]["query_patterns"]
        }
        draft = store.read_json(f"{base}/schema_draft_group_{idx}.json")
        got = {qid for ap in draft["access_patterns"] for qid in ap["query_ids"]}
        out[idx] = (own, got)
    return out


def test_the_design_ran_every_group_at_once(drafts) -> None:
    assert len(drafts) == GROUPS
    owns = [own for own, _ in drafts.values()]
    assert all(owns) and not set.intersection(*owns)


def test_each_draft_holds_exactly_its_own_groups_queries(drafts) -> None:
    for idx, (own, got) in sorted(drafts.items()):
        assert got == own, (
            f"group {idx}: {len(got & own)} of its {len(own)} queries, "
            f"{len(got - own)} from another group"
        )


def test_each_groups_numbers_check_reads_its_own_input(drafts) -> None:
    """The numbers check gets the metrics of the group its thread designs, never
    another group's, and never none (#1054 review)."""
    assert len(checked_metrics) == GROUPS
    for thread, metrics in checked_metrics.items():
        assert metrics is not None
        assert metrics == read_metrics[thread]
    assert len({json.dumps(m, sort_keys=True) for m in checked_metrics.values()}) == GROUPS
