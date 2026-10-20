"""The library ingest records the source engine ``detect_source_engine`` reads (#1177).

``core.ingest_offline_collection`` used to guess on its own ("postgres" in the
version string, else MySQL), so every SQL Server, Oracle and DB2 collection a
library caller ingested was recorded as MySQL: triage then selected only Aurora
MySQL and skipped Aurora PostgreSQL, and the #381 Aurora choice never ran. The same
collection through ``scripts/run_assessment.py`` was detected correctly.

Each case below is the trailgear sample (a SQL Server collection) with its metadata
replaced, so the only thing that differs between cases is what the metadata says.
"""

from __future__ import annotations

import copy
import json
import zipfile
from pathlib import Path

import pytest

from src.atx_orchestrator import core
from src.storage.local_store import LocalArtifactStore
from src.tools.database.collection_text import extract_json_object

_REPO = Path(__file__).resolve().parents[3]
_TRAILGEAR_ZIP = _REPO / "docs" / "examples" / "trailgear" / "trailgear.zip"
_AURORA = {"aurora_mysql", "aurora_postgresql"}


@pytest.fixture(scope="module")
def trailgear() -> dict:
    with zipfile.ZipFile(_TRAILGEAR_ZIP) as z:
        text = z.read("trailgear-collection.json").decode("utf-8")
    data: dict = json.loads(extract_json_object(text))
    return data


def _with_metadata(base: dict, **metadata: object) -> dict:
    raw = copy.deepcopy(base)
    raw["metadata"] = {"database_name": "trailgear", "database_size_gb": 27, **metadata}
    return raw


# (case id, metadata, expected source engine, expected Aurora agents selected)
_CASES = [
    (
        "sqlserver-engine",
        {"engine": "sqlserver", "product_version": "15.0.4322.2"},
        "sqlserver",
        _AURORA,
    ),
    (
        "sqlserver-banner",
        {"version": "Microsoft SQL Server 2019 (RTM-CU22) - 15.0.4322.2 (X64)"},
        "sqlserver",
        _AURORA,
    ),
    ("oracle-engine", {"engine": "oracle", "version": "19.0.0.0.0"}, "oracle", _AURORA),
    (
        "oracle-banner",
        {"version": "Oracle Database 19c Enterprise Edition Release 19.0.0.0.0"},
        "oracle",
        _AURORA,
    ),
    ("db2-engine", {"engine": "db2", "version": "11.5.8.0"}, "db2", _AURORA),
    ("db2-banner", {"version": "DB2 v11.5.8.0"}, "db2", _AURORA),
    ("mariadb-engine", {"engine": "mariadb", "version": "10.11.6-MariaDB"}, "mariadb", None),
    # scripts/collect-mysql.sql writes only VERSION(): the banner alone names MariaDB.
    ("mariadb-banner", {"version": "10.11.6-MariaDB-1:10.11.6+maria~ubu2204"}, "mariadb", None),
    ("postgresql-banner", {"version": "PostgreSQL 16.10 on x86_64"}, "postgresql", None),
    ("postgres-alias", {"engine": "postgres", "version": "16.10"}, "postgresql", None),
    ("mysql-version", {"version": "8.0.36"}, "mysql", None),
]


def _core_path(tmp_path: Path, raw: dict) -> tuple[dict, dict]:
    store = LocalArtifactStore(base_dir=str(tmp_path / "core"))
    core.ingest_offline_collection(store, "job-core", "trailgear", raw)
    core.run_triage_core("job-core", "trailgear", store)
    return (
        store.read_json("trailgear/job-core/collector/output.json"),
        store.read_json("trailgear/job-core/referee-triage/triage.json"),
    )


def _script_path(tmp_path: Path, raw: dict, monkeypatch: pytest.MonkeyPatch) -> tuple[dict, dict]:
    import scripts.run_assessment as run_assessment

    monkeypatch.chdir(tmp_path)  # phase_collect writes .modernizer-state.json here
    collection = tmp_path / "trailgear-collection.json"
    collection.write_text(json.dumps(raw), encoding="utf-8")
    store = LocalArtifactStore(base_dir=str(tmp_path / "script"))
    job_id, db = run_assessment.phase_collect(str(collection), "trailgear", store)
    run_assessment.phase_triage(store, job_id, db)
    return (
        store.read_json(f"{db}/{job_id}/collector/output.json"),
        store.read_json(f"{db}/{job_id}/referee-triage/triage.json"),
    )


def _source(collector: dict) -> dict:
    src: dict = collector["metadata"]["source_database"]
    return src


def _selected(triage: dict) -> set[str]:
    return {a["agent_type"] for a in triage["selected_agents"]}


@pytest.mark.parametrize(
    ("metadata", "engine", "aurora"),
    [pytest.param(m, e, a, id=i) for i, m, e, a in _CASES],
)
def test_core_ingest_records_the_detected_source_engine(
    tmp_path: Path, trailgear: dict, metadata: dict, engine: str, aurora: set[str] | None
) -> None:
    collector, triage = _core_path(tmp_path, _with_metadata(trailgear, **metadata))

    assert _source(collector)["engine"] == engine
    if aurora is not None:
        # A heterogeneous source: both Aurora engines compete (#381).
        assert _selected(triage) & _AURORA == aurora


@pytest.mark.parametrize(
    ("metadata", "engine"),
    [pytest.param(m, e, id=i) for i, m, e, _ in _CASES],
)
def test_core_ingest_and_run_assessment_agree(
    tmp_path: Path,
    trailgear: dict,
    monkeypatch: pytest.MonkeyPatch,
    metadata: dict,
    engine: str,
) -> None:
    raw = _with_metadata(trailgear, **metadata)
    core_collector, core_triage = _core_path(tmp_path, raw)
    script_collector, script_triage = _script_path(tmp_path, raw, monkeypatch)

    core_src, script_src = _source(core_collector), _source(script_collector)
    assert core_src["engine"] == script_src["engine"] == engine
    for field in ("version", "database_name", "database_size_gb"):
        assert core_src.get(field) == script_src.get(field), field
    assert _selected(core_triage) == _selected(script_triage)
    assert {a["agent_type"] for a in core_triage["skipped_agents"]} == {
        a["agent_type"] for a in script_triage["skipped_agents"]
    }


def test_trailgear_sample_through_both_paths(
    tmp_path: Path, trailgear: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unchanged trailgear sample: SQL Server on both paths, the same queries, the
    same triage, and both Aurora engines selected."""
    core_collector, core_triage = _core_path(tmp_path, trailgear)
    script_collector, script_triage = _script_path(tmp_path, trailgear, monkeypatch)

    assert _source(core_collector)["engine"] == _source(script_collector)["engine"] == "sqlserver"
    assert _source(core_collector)["version"] == _source(script_collector)["version"]

    def _queries(collector: dict) -> list[tuple[str, str]]:
        return sorted(
            (q["query_id"], q.get("query_type") or "")
            for q in collector["queries"]["query_patterns"]
        )

    assert _queries(core_collector) == _queries(script_collector)
    assert _selected(core_triage) == _selected(script_triage)
    assert _selected(core_triage) >= _AURORA


def _wordpress() -> dict:
    with zipfile.ZipFile(_REPO / "docs" / "examples" / "wordpress" / "wordpress.zip") as z:
        text = z.read("wordpress-collection.json").decode("utf-8")
    data: dict = json.loads(extract_json_object(text))
    return data


def _routing(tmp_path: Path, raw: dict, monkeypatch: pytest.MonkeyPatch) -> tuple[str, dict]:
    """Source engine, and per query its engine after the first assignment (v1) and after
    the reality check (v2), through the deterministic pipeline."""
    import scripts.run_assessment as run_assessment

    monkeypatch.chdir(tmp_path)
    collection = tmp_path / "wordpress-collection.json"
    collection.write_text(json.dumps(raw), encoding="utf-8")
    store = LocalArtifactStore(base_dir=str(tmp_path / "artifacts"))
    job_id, db = run_assessment.phase_collect(str(collection), "wordpress", store)
    selected = run_assessment.phase_triage(store, job_id, db)
    run_assessment.phase_analysis(store, job_id, db, selected)
    run_assessment.phase_assignment(store, job_id, db)
    run_assessment.phase_reality_check(store, job_id, db, "none")
    collector = store.read_json(f"{db}/{job_id}/collector/output.json")
    routing: dict = {"selected": sorted(selected)}
    for version in ("v1", "v2"):
        assignment = store.read_json(f"{db}/{job_id}/assignment/{version}/assignment.json")
        routing[version] = {
            qa["query_id"]: (qa.get("assigned_engine"), tuple(qa.get("tied_engines") or ()))
            for qa in assignment["query_assignments"]
        }
    return _source(collector)["engine"], routing


def test_a_mariadb_banner_routes_exactly_as_mysql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Detecting MariaDB from its version banner (#1177 review) moves no query: the
    catalog's Aurora MySQL lists both mysql and mariadb as source engines. wordpress,
    recorded as mysql, against the same collection with a MariaDB banner."""
    base = _wordpress()
    mariadb = copy.deepcopy(base)
    mariadb["metadata"] = {**base["metadata"], "version": "10.11.6-MariaDB"}
    (tmp_path / "mysql").mkdir()
    (tmp_path / "mariadb").mkdir()

    mysql_engine, mysql_routing = _routing(tmp_path / "mysql", base, monkeypatch)
    mariadb_engine, mariadb_routing = _routing(tmp_path / "mariadb", mariadb, monkeypatch)

    assert (mysql_engine, mariadb_engine) == ("mysql", "mariadb")
    assert mariadb_routing == mysql_routing
    assert len(mysql_routing["v1"]) >= 50
