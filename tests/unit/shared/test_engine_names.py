"""Engine display names are the one shared, AWS-branded source of truth (#221).

``src.report.analysis_report.ENGINE_LABELS`` and the hand-kept mirror in
``src/ui/src/utils/ExportReport.js`` had drifted into non-brand casing
("Elasticache", "AuroraPostgresql", "AuroraMySQL"); ``src.report.pptx_report``'s
own copy had the correct names all along. This pins the shared module against
``pptx_report.ENGINE_LABEL`` for the engines both cover, so the two cannot drift
again without a test failing.
"""

from __future__ import annotations

from src.report.pptx_report import ENGINE_LABEL as PPTX_ENGINE_LABEL
from src.shared.engine_names import ENGINE_DISPLAY_NAMES, display_engine


def test_matches_pptx_report_for_every_engine_it_covers() -> None:
    for engine, name in PPTX_ENGINE_LABEL.items():
        assert ENGINE_DISPLAY_NAMES[engine] == name


def test_covers_engines_pptx_report_does_not() -> None:
    for engine, name in (("neptune", "Neptune"), ("keyspaces", "Keyspaces"), ("aurora", "Aurora")):
        assert ENGINE_DISPLAY_NAMES[engine] == name


def test_display_engine_known_ids() -> None:
    assert display_engine("aurora_mysql") == "Aurora MySQL"
    assert display_engine("aurora_postgresql") == "Aurora PostgreSQL"
    assert display_engine("elasticache") == "ElastiCache"
    assert display_engine("documentdb") == "DocumentDB"
    assert display_engine("dynamodb") == "DynamoDB"
    assert display_engine("opensearch") == "OpenSearch"


def test_display_engine_falls_back_to_a_titlecased_id() -> None:
    assert display_engine("some_future_engine") == "Some Future Engine"


def test_display_engine_passes_through_empty_and_falsy() -> None:
    assert display_engine("") == ""
