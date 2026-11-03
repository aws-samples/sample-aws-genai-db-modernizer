# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every request the cookbook prints runs as written (#307).

Reads the ``json`` blocks of ``docs/guides/context-graph-query-cookbook.md``
(not the catalog), so the page a user copies from is what is tested: each
request passes the raw endpoint's filter and runs on graphs rebuilt from real
job folders, with its example parameters, and no value comes back NaN or as an
empty node label.
"""

from __future__ import annotations

import json
import math
import re
import shutil
from pathlib import Path

import pytest

from src.graph.cypher_guard import bound_result_rows
from src.graph.populators import rebuild_graph
from src.graph.schema import initialize_schema
from src.graph.store import MAX_RESULT_ROWS, GraphStore
from src.storage.local_store import LocalArtifactStore

REPO = Path(__file__).resolve().parents[2]
COOKBOOK = REPO / "docs" / "guides" / "context-graph-query-cookbook.md"
FIXTURES = REPO / "tests" / "fixtures"
JOBS = {
    "old_jobs": FIXTURES / "old_jobs" / "v0.4.0-beta.2" / "wordpress" / "aa4e840a",
    "real_runs": FIXTURES / "real_runs" / "wordpress-both" / "job",
}


def _requests() -> list[tuple[str, dict]]:
    text = COOKBOOK.read_text(encoding="utf-8")
    found = []
    for heading, body in re.findall(r"### ([^\n]+)\n(.*?)(?=\n### |\n## |\Z)", text, re.S):
        for block in re.findall(r"```json\n(\{\"cypher\".*?\})\n```", body, re.S):
            found.append((heading.split(".")[0], json.loads(block)))
    return found


REQUESTS = _requests()


@pytest.fixture(scope="module")
def graphs(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("cookbook")
    stores = {}
    for name, job in JOBS.items():
        base = tmp / name
        shutil.copytree(job, base / "wordpress" / "job")
        store = GraphStore(str(tmp / f"{name}.lbug"))
        initialize_schema(store)
        rebuild_graph("wordpress", "job", LocalArtifactStore(str(base)), store)
        stores[name] = store
    yield stores
    for store in stores.values():
        store.close()


@pytest.mark.passes_on_main
def test_the_cookbook_prints_every_query():
    refs = [ref for ref, _ in REQUESTS]
    assert {str(n) for n in range(1, 11)} | set("ABCDEF") <= set(refs)


@pytest.mark.parametrize("fixture", sorted(JOBS))
@pytest.mark.parametrize("ref,request_body", REQUESTS, ids=[r for r, _ in REQUESTS])
def test_every_printed_request_runs_and_returns_real_values(graphs, fixture, ref, request_body):
    cypher = bound_result_rows(request_body["cypher"], MAX_RESULT_ROWS)
    rows = graphs[fixture].query(cypher, request_body.get("params") or None)
    for row in rows:
        for column, value in row.items():
            assert not (isinstance(value, float) and math.isnan(value)), (ref, column, row)
    if ref == "1":
        assert rows and all(r["type"] for r in rows), rows
