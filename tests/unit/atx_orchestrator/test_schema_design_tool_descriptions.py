# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The six schema-design tools reach the model with their shared description.

Strands reads a tool's description and parameter docs when it decorates the
function, so a docstring assigned after ``@tool`` is never sent: the model saw
each tool described only by its own name.
"""

from __future__ import annotations

import pytest

from src.atx_orchestrator import tools

SCHEMA_TOOLS = (
    ("run_schema_design_dynamodb_via_a2a", "DynamoDB"),
    ("run_schema_design_documentdb_via_a2a", "DocumentDB"),
    ("run_schema_design_elasticache_via_a2a", "ElastiCache"),
    ("run_schema_design_opensearch_via_a2a", "OpenSearch"),
    ("run_schema_design_aurora_pg_via_a2a", "Aurora PostgreSQL"),
    ("run_schema_design_aurora_mysql_via_a2a", "Aurora MySQL"),
)


@pytest.mark.parametrize(("name", "label"), SCHEMA_TOOLS)
def test_tool_spec_carries_the_shared_description(name: str, label: str) -> None:
    spec = getattr(tools, name).tool_spec
    assert spec["name"] == name
    assert spec["description"].startswith(f"Design the {label} target schema")
    assert "in parallel" in spec["description"]


@pytest.mark.parametrize(("name", "label"), SCHEMA_TOOLS)
def test_tool_spec_documents_both_parameters(name: str, label: str) -> None:
    properties = getattr(tools, name).tool_spec["inputSchema"]["json"]["properties"]
    assert properties["job_id"]["description"] == "Unique job identifier."
    assert properties["database_name"]["description"] == "Source database name."
