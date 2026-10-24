"""The model entry points an ``assess()`` run with ``deterministic=False`` reaches, and
what tests put in their place (tests never call a model).

- the reality check's review: accept every consolidation, no model summary;
- schema design (``run_schema_design_auto``, which ``run_schema_design_core`` calls):
  on the wordpress sample, the designed wordpress job's designs
  (``tests/e2e/designed_run.py``; DynamoDB's with one global secondary index added, so
  the stable design block has one) and a small DocumentDB design
  (:func:`write_documentdb_design`); on any other database, a skipped placeholder, as a
  designer writes when it has nothing to design. The database
  :data:`FLAGGED_DATABASE` (the wordpress sample under another name) gets the same
  designs, with its first DynamoDB design reporting one statement unsupported
  (:func:`report_unsupported`), so a consumer fixture reaches the post-schema routing;
- synthesis's executive summary: no model answer, so the deterministic one stays.

A phase that adds a model call adds its entry point to :data:`MODEL_ENTRY_POINTS`. The
Bedrock guard in ``tests/conftest.py`` still refuses any call this list misses.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterator
from contextlib import ExitStack, contextmanager
from typing import Any
from unittest.mock import MagicMock, patch

REALITY_CHECK = "src.agents.referee.reality_check_handler"
SYNTHESIS = "src.agents.referee.synthesis_handler"
DESIGNER = "src.agents.schema_design.handler.run_schema_design_auto"


#: The global secondary index the fake DynamoDB design adds to the table of its first
#: access pattern, which it then serves.
FAKE_GSI = {
    "gsi_name": "ByStatus",
    "partition_key": [{"attribute_name": "status", "attribute_type": "S"}],
    "sort_key": [{"attribute_name": "updated_at", "attribute_type": "S"}],
    "projection": "INCLUDE",
    "projected_attributes": ["title"],
    "item_count": 10,
    "item_size_bytes": 100,
}


def write_dynamodb_design_with_gsi(
    store: Any, database_name: str, job_id: str, assignment_version: int
) -> None:
    from tests.e2e.designed_run import write_dynamodb_design

    write_dynamodb_design(store, database_name, job_id, assignment_version)
    key = f"{database_name}/{job_id}/schema-dynamodb/v{assignment_version}/schema_output.json"
    design = store.read_json(key)
    patterns = design.get("access_patterns") or []
    if patterns:
        patterns[0]["gsi_name"] = FAKE_GSI["gsi_name"]
        for table in design["table_definitions"]:
            if table["table_name"] == patterns[0]["table_name"]:
                table["gsis"] = [*table.get("gsis", []), dict(FAKE_GSI)]
    store.write_json(key, design)


def _bare(table: str) -> str:
    return table.rsplit(".", 1)[-1]


def write_documentdb_design(
    store: Any, database_name: str, job_id: str, assignment_version: int
) -> None:
    """A DocumentDB design for the version's DocumentDB statements: one collection per
    source table they read, the first embedding the second (and referencing the third),
    an index on the first, and one access pattern per statement."""
    from src.contracts.documentdb_model_output import DocumentDBModelOutputContract

    assignment = store.read_json(
        f"{database_name}/{job_id}/assignment/v{assignment_version}/assignment.json"
    )
    queries = [
        qa
        for qa in assignment["query_assignments"]
        if qa["assigned_engine"] == "documentdb" and qa.get("in_scope", True)
    ]
    tables = sorted({str(t) for qa in queries for t in qa.get("source_tables") or []})
    names = [_bare(t) for t in tables] or ["documents"]
    first = names[0]
    collections = [
        {
            "collection_name": name,
            "source_tables": [table],
            "embedded_entities": (
                [
                    {
                        "source_table": tables[1],
                        "embed_path": names[1],
                        "strategy": "embed",
                        "avg_array_length": 3,
                        "max_array_length_estimate": 50,
                        "rationale": "Read with its parent.",
                    }
                ]
                if name == first and len(tables) > 1
                else []
            ),
            "referenced_collections": names[2:3] if name == first else [],
            "indexes": (
                [
                    {
                        "index_name": f"idx_{first}_status",
                        "keys": {"status": 1, "updated_at": -1},
                        "index_type": None,
                        "purpose": "Lists by status, newest first.",
                        "source_query_ids": [str(q["query_id"]) for q in queries[:1]],
                    }
                ]
                if name == first
                else []
            ),
            "document_examples": [{"_id": 1}],
            "estimated_avg_doc_size_kb": 2,
            "estimated_max_doc_size_kb": 40,
        }
        for table, name in zip(tables, names, strict=True)
    ]
    by_table = dict(zip(tables, names, strict=True))
    design = DocumentDBModelOutputContract.model_validate(
        {
            "job_id": job_id,
            "source_database": database_name,
            "collections": collections,
            "access_patterns": [
                {
                    "pattern_id": f"DOC-AP-{i + 1}",
                    "description": "Read the documents of one parent",
                    "operation": "find",
                    "collection_name": by_table[str(qa["source_tables"][0])],
                    "query_filter": {"status": "?"},
                    "index_used": f"idx_{first}_status",
                    "source_query_ids": [str(qa["query_id"])],
                    "source_tables": [str(t) for t in qa["source_tables"]],
                    "design_rps": 1.0,
                }
                for i, qa in enumerate(q for q in queries if q.get("source_tables"))
            ],
            "trade_offs": [{"description": "Embed the children", "impact": "One read."}],
            "validation_passed": True,
        }
    )
    key = f"{database_name}/{job_id}/schema-documentdb/v{assignment_version}/schema_output.json"
    store.write_json(key, design.model_dump(mode="json"))


#: A database the fake designer designs as wordpress, with the first DynamoDB design
#: reporting one statement unsupported (``assess_consumer_fixtures.json``'s
#: ``wordpress_terminal``: in an engine scope with no other owner, it stays terminal).
FLAGGED_DATABASE = "wordpress_terminal"

#: The databases the fake designer writes the designed wordpress job's designs for.
DESIGNED_DATABASES = ("wordpress", FLAGGED_DATABASE)


def report_unsupported(
    store: Any, database_name: str, job_id: str, version: int, skip: Collection[str] = ()
) -> str:
    """Make the DynamoDB design at ``version`` report one statement unsupported, and
    return its id: the first DynamoDB read (by id) in no placement unit, so it moves
    alone, and not in ``skip``."""
    from src.agents.referee.write_groups import assignment_placement_groups

    prefix = f"{database_name}/{job_id}"
    assignment = store.read_json(f"{prefix}/assignment/v{version}/assignment.json")
    grouped = {m for g in assignment_placement_groups(assignment) if len(g) > 1 for m in g}
    types = {
        q["query_id"]: str(q.get("query_type") or "").upper()
        for q in store.read_json(f"{prefix}/collector/output.json")["queries"]["query_patterns"]
    }
    flagged = sorted(
        qa["query_id"]
        for qa in assignment["query_assignments"]
        if qa["assigned_engine"] == "dynamodb"
        and qa.get("in_scope", True)
        and qa["query_id"] not in grouped
        and qa["query_id"] not in skip
        and types.get(qa["query_id"]) == "SELECT"
    )[0]
    key = f"{prefix}/schema-dynamodb/v{version}/schema_output.json"
    design = store.read_json(key)
    design["unsupported_patterns"] = [
        {
            "query_ids": [flagged],
            "pattern_type": "aggregation",
            "recommendation": "needs a server-side aggregation",
        }
    ]
    store.write_json(key, design)
    return str(flagged)


def write_fake_design(
    job_id: str, database_name: str, target_type: str, store: Any, assignment_version: int
) -> None:
    from src.agents.schema_design.output_contract import write_schema_output
    from src.contracts.skipped_placeholders import SkippedSchemaOutput
    from tests.e2e.designed_run import write_aurora_mysql_design, write_elasticache_design

    writers: dict[str, Callable[..., Any]] = {
        "dynamodb": write_dynamodb_design_with_gsi,
        "elasticache": write_elasticache_design,
        "aurora_mysql": write_aurora_mysql_design,
        "documentdb": write_documentdb_design,
    }
    if database_name in DESIGNED_DATABASES and target_type in writers:
        writers[target_type](store, database_name, job_id, assignment_version)
        if database_name == FLAGGED_DATABASE and target_type == "dynamodb":
            report_unsupported(store, database_name, job_id, assignment_version)
        return
    placeholder = SkippedSchemaOutput(
        target_type=target_type,
        reason="No design in this test",
        assignment_version=assignment_version,
    )
    key = f"{database_name}/{job_id}/schema-{target_type}/v{assignment_version}/schema_output.json"
    write_schema_output(store, key, placeholder.model_dump(mode="json"), target_type)


def fake_designer(**kw: Any) -> Any:
    # ``run_schema_design_core`` passes every argument by name.
    write_fake_design(
        kw["job_id"], kw["database_name"], kw["target_type"], kw["store"], kw["assignment_version"]
    )


#: ``(target, value)``: a callable value is the patch's side effect, any other value
#: its return value.
MODEL_ENTRY_POINTS: tuple[tuple[str, Any], ...] = (
    (f"{REALITY_CHECK}.require_valid_guardrail_config", None),
    (f"{REALITY_CHECK}.validate_consolidations", []),
    (f"{REALITY_CHECK}._generate_executive_summary", None),
    (DESIGNER, fake_designer),
    (f"{SYNTHESIS}.require_valid_guardrail_config", None),
    (f"{SYNTHESIS}.generate_executive_summary", None),
)


@contextmanager
def models_patched() -> Iterator[dict[str, MagicMock]]:
    """Every entry point of :data:`MODEL_ENTRY_POINTS` patched; the mocks, by target."""
    mocks: dict[str, MagicMock] = {}
    with ExitStack() as stack:
        for target, value in MODEL_ENTRY_POINTS:
            mock = (
                MagicMock(side_effect=value) if callable(value) else MagicMock(return_value=value)
            )
            mocks[target] = stack.enter_context(patch(target, mock))
        yield mocks
