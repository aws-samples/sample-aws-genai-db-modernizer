"""A schema-design input saved before #293 still loads and drafts as it did.

``AgentIndex.index_type`` arrived with #293. An input written earlier has no such key on
its indexes: it must load, draft every index as a B-tree on both Aurora engines, raise no
index note and show the index line without a method.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.contracts.schema_design_input import AgentCollectorInput
from src.tools.schema.aurora_common.delta_merge import AuroraDesignBase
from src.tools.schema.aurora_common.design_view import build_design_view

OLD_INPUT = {
    "contract_version": "3.0",
    "job_id": "j",
    "source_database_name": "shop",
    "source_database_engine": "postgresql",
    "collection_timestamp": "2026-01-01T00:00:00Z",
    "tables": [
        {
            "table_id": "shop.topics",
            "table_name": "topics",
            "row_count": 1,
            "primary_key": ["id"],
            "columns": [
                {"column_name": "id", "data_type": "bigint", "nullable": False},
                {"column_name": "slug", "data_type": "text", "nullable": True},
            ],
            "indexes": [{"index_name": "idx_slug", "columns": ["slug"], "is_unique": True}],
        }
    ],
    "queries": {"query_patterns": []},
}


def _old_input(tmp_path: Path) -> AgentCollectorInput:
    path = tmp_path / "agent_input.json"
    path.write_text(json.dumps(OLD_INPUT))
    data = json.loads(path.read_text())
    assert "index_type" not in data["tables"][0]["indexes"][0]
    return AgentCollectorInput.model_validate(data)


@pytest.mark.parametrize(
    ("engine", "statement"),
    [
        ("aurora_postgresql", 'CREATE UNIQUE INDEX "idx_slug" ON "topics" ("slug");'),
        ("aurora_mysql", "CREATE UNIQUE INDEX `idx_slug` ON `topics` (`slug`);"),
    ],
)
def test_an_index_without_a_method_drafts_as_a_btree(tmp_path: Path, engine: str, statement: str):
    collector = _old_input(tmp_path)
    assert collector.tables[0].indexes[0].index_type is None

    base = AuroraDesignBase.from_inputs(engine, collector)
    draft = base.generate()

    assert draft.tables[0].index_sql == [statement]
    assert draft.index_notes == []
    assert build_design_view(base, collector)["tables"][0]["indexes"] == ["idx_slug UNIQUE (slug)"]
