"""#568 review of #562 (Important 2): the merged DynamoDB/DocumentDB wave's
``table_count`` is the union of both engines' served tables; crediting that
union to every engine in ``engines`` double counted.

On the discourse ``--llm-mode none`` run, wave 3 (merged) served 200 tables
(118 DynamoDB, 82 DocumentDB, no overlap). Before this fix,
``_architecture_engines`` added the wave's whole ``table_count`` (200) to
*each* engine, so the decision report, deck and PDF all showed "DynamoDB 200
source tables" and "DocumentDB 200 source tables", and the "Tables migrate"
tile summed to 400 against "Analyzed 311 source tables" -- a deliverable
total that exceeded the number of tables that exist.

``MigrationWave.table_counts_by_engine``/``owned_counts_by_engine`` (#568
review) now carry each engine's own served and owned counts for a
multi-engine wave, so ``_architecture_engines`` can credit each engine only
its own share.

Fixture: ``discourse_merged_wave_report.json``, the real synthesis report
from ``uv run python scripts/run_assessment.py --all -y --llm-mode none`` on
the bundled discourse sample (post-#568-review code), with ``query_groups``
emptied and ``risk_assessment.risks`` truncated to keep the fixture small --
neither is read by anything this test exercises.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from src.report import architecture, renderers

FIXTURE = Path(__file__).parent / "fixtures" / "discourse_merged_wave_report.json"


@pytest.fixture(scope="module")
def report() -> dict[str, Any]:
    data: dict = json.loads(FIXTURE.read_text())
    return data


def test_fixture_has_a_merged_wave_with_no_table_overlap(report: dict[str, Any]) -> None:
    merged = next(w for w in report["migration_waves"] if len(w["engines"]) > 1)
    assert merged["engines"] == ["dynamodb", "documentdb"]
    assert merged["table_count"] == 200
    assert merged["table_counts_by_engine"] == {"dynamodb": 118, "documentdb": 82}
    assert merged["owned_counts_by_engine"] == {"dynamodb": 108, "documentdb": 73}


def test_architecture_engines_credits_each_engine_its_own_share(report: dict[str, Any]) -> None:
    rows = {e["engine"]: e for e in architecture._architecture_engines(report)}
    # Scope shows each engine's own SERVED count -- never the union (200/200) --
    # and, since #1169, how many of those migrate and how many are kept in sync.
    assert rows["dynamodb"]["scope"] == "118 source tables served: 108 migrate, 10 kept in sync"
    assert rows["documentdb"]["scope"] == "82 source tables served: 73 migrate, 9 kept in sync"
    # "Tables migrate" counts only the OWNED (physically moving) subset --
    # a served-but-not-owned table stays on its real owner, kept in sync.
    assert rows["dynamodb"]["migrates"] == 108
    assert rows["documentdb"]["migrates"] == 73


def test_no_deliverable_total_exceeds_the_source_table_count(report: dict[str, Any]) -> None:
    """The explicit #568 review ask: no deliverable's aggregate table total
    may exceed the number of source tables that actually exist."""
    source_total = report["source_table_count"]
    assert source_total == 311

    rows = architecture._architecture_engines(report)
    migrated_total = sum(e["migrates"] for e in rows if e["role"] == "Migration target")
    assert migrated_total <= source_total
    assert migrated_total == 181  # 108 (dynamodb) + 73 (documentdb), the real owned split

    md = renderers.render_engineering_report_md(report)
    total_n = int(
        re.search(r"^\| Total \| \| [^|]* \| (\d+) tables? migrates? \|", md, re.M).group(1)
    )
    assert total_n <= source_total
    assert total_n == migrated_total

    # The engineering report's own per-engine Scope-equivalent (the roadmap
    # wave card) must also never claim more tables than exist.
    for m in re.finditer(r"(\d+) source tables?:", md):
        assert int(m.group(1)) <= source_total


def test_engineering_report_and_architecture_rows_agree_on_the_split(
    report: dict[str, Any],
) -> None:
    """The two per-engine Scope cells, read off the rendered engineering report,
    must match the architecture rows' own per-engine figures -- the
    "deliverables agree" invariant, specifically for the merged wave."""
    md = renderers.render_engineering_report_md(report)
    dynamo_row = re.search(
        r"^\| DynamoDB \| Migration target \| [\d.]+% \| (\d+) source tables? served:", md, re.M
    )
    doc_row = re.search(
        r"^\| DocumentDB \| Migration target \| [\d.]+% \| (\d+) source tables? served:",
        md,
        re.M,
    )
    assert dynamo_row is not None, md
    assert doc_row is not None, md
    assert int(dynamo_row.group(1)) == 118
    assert int(doc_row.group(1)) == 82

    rows = {e["engine"]: e for e in architecture._architecture_engines(report)}
    assert rows["dynamodb"]["scope"].startswith("118 source tables served:")
    assert rows["documentdb"]["scope"].startswith("82 source tables served:")
