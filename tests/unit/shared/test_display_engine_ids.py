"""Raw engine ids in model-written design prose read as engine names (#1574).

A scoped run's DynamoDB designer wrote "Full-text matching stays on aurora_postgresql,
which runs the other part of this work" and "... is rebuilt from raw_data on
aurora_postgresql when a post is indexed" into its trade-offs; the wording guard rightly
flagged both in report.json and the engineering report. Synthesis rewrites them at the
design prose seam, and the read-time step does the same for an older report.
"""

from __future__ import annotations

import pytest

from src.shared.design_text import design_prose_report
from src.shared.engine_names import display_engine_ids
from src.tools.validation.customer_text_guard import scan_text

SEEN = [
    (
        "Full-text matching stays on aurora_postgresql, which runs the other part of this work.",
        "Full-text matching stays on Aurora PostgreSQL, which runs the other part of this work.",
    ),
    (
        "The search_data value is rebuilt from raw_data on aurora_postgresql when a post is "
        "indexed.",
        "The search_data value is rebuilt from raw_data on Aurora PostgreSQL when a post is "
        "indexed.",
    ),
    ("Keep it on dynamodb.", "Keep it on DynamoDB."),
    ("aurora_mysql and documentdb both read it.", "Aurora MySQL and DocumentDB both read it."),
]


@pytest.mark.parametrize(("raw", "named"), SEEN)
def test_a_raw_engine_id_in_prose_becomes_its_name(raw: str, named: str) -> None:
    assert display_engine_ids(raw) == named
    assert [f for f in scan_text(display_engine_ids(raw)) if f.kind == "engine_id"] == []


@pytest.mark.parametrize(
    "kept",
    [
        "Deny it with an IAM policy with a dynamodb:Attributes condition.",
        "Grant dynamodb:PutItem and elasticache:CreateCacheCluster only.",
        "The table arn:aws:dynamodb:us-east-1:123456789012:table/users is the target.",
        "Read `aurora_postgresql` from the config key.",
        "The file artifacts/discourse/j1/analysis-dynamodb/analysis.json holds it.",
        "Set engine=dynamodb.json in the manifest.",
        "No engine is named here.",
        "",
    ],
)
def test_code_spans_iam_names_arns_and_paths_are_untouched(kept: str) -> None:
    assert display_engine_ids(kept) == kept


def test_the_design_prose_seam_names_the_engines() -> None:
    from src.agents.referee.synthesis_prose import clean_schema_design_prose

    schema = {
        "trade_offs": [
            {"description": "Search stays relational.", "impact": SEEN[0][0], "engine": "dynamodb"}
        ],
        "migration_notes": [{"object_name": "t", "application_logic_required": SEEN[1][0]}],
    }
    out, _ = clean_schema_design_prose("dynamodb", schema)
    assert out["trade_offs"][0]["impact"] == SEEN[0][1]
    assert out["trade_offs"][0]["engine"] == "dynamodb"  # an id field is left as it is
    assert out["migration_notes"][0]["application_logic_required"] == SEEN[1][1]


def test_an_older_report_reads_the_names_in_its_trade_offs_and_design_summaries() -> None:
    report = {
        "trade_offs": [
            {"description": "D.", "impact": SEEN[0][0], "engine": "dynamodb"},
            {"description": "Kept on aurora_postgresql.", "impact": "x", "engine": "reality-check"},
        ],
        "schema_designs": {
            "dynamodb": {"trade_offs": [{"description": "D.", "impact": SEEN[1][0]}]}
        },
    }
    out = design_prose_report(report)
    assert out["trade_offs"][0]["impact"] == SEEN[0][1]
    assert out["trade_offs"][1] == report["trade_offs"][1]  # the reality check's, as written
    assert out["schema_designs"]["dynamodb"]["trade_offs"][0]["impact"] == SEEN[1][1]
    assert design_prose_report(out) is out


@pytest.mark.parametrize(
    "skill",
    [
        "aurora_mysql-data-modeling.md",
        "aurora_postgresql-data-modeling.md",
        "documentdb-data-modeling.md",
        "dynamodb-data-modeling.md",
        "elasticache-data-modeling.md",
        "opensearch-index-modeling.md",
    ],
)
def test_every_designer_skill_says_to_name_engines_never_ids(skill: str) -> None:
    from pathlib import Path

    text = (Path(__file__).resolve().parents[3] / "src" / "skills" / skill).read_text()
    text = " ".join(text.split())
    assert "Never write an engine id in prose" in text
    assert "`aurora_postgresql` is Aurora PostgreSQL" in text
