"""The collector keeps the buffer-cache hit ratio and the host's CPU and memory (#1197).

The offline parser computes the instance-wide buffer-cache figures from every
collection script's ``global_stats`` (MySQL, PostgreSQL, SQL Server and Oracle all map
theirs to the same keys) and passes the script's ``os_stats`` through; the collector
output contract (3.5) now carries both in ``metrics.source_host`` instead of dropping
them, on every path that turns a raw collection into a collector output.
"""

from __future__ import annotations

import json
import subprocess  # nosec B404 -- runs this repo's own run_collect.py script with fixed argv
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

from scripts import run_assessment
from src.agents.collector.mysql_collector import _build_source_host
from src.contracts.collector_output import CollectorOutputContract
from src.shared.contract_versioning import current_version
from src.storage.local_store import LocalArtifactStore
from src.tools.database.collection_text import extract_json_object
from src.tools.database.offline_parser import parse_offline_collection

REPO = Path(__file__).resolve().parents[3]


def _sample(name: str) -> dict[str, Any]:
    with zipfile.ZipFile(REPO / "docs" / "examples" / name / f"{name}.zip") as z:
        text = z.read(f"{name}-collection.json").decode("utf-8")
    data: dict[str, Any] = json.loads(extract_json_object(text, source=name))
    return data


def _write_sample(name: str, dest: Path) -> Path:
    path = dest / f"{name}-collection.json"
    path.write_text(json.dumps(_sample(name)))
    return path


# --- the builder ------------------------------------------------------------------


def test_the_hit_ratio_is_read_from_the_counts_not_the_rounded_percent() -> None:
    # wordpress: 5,026,219,966 logical reads, 3,501 from disk. The parser's own
    # percent is rounded to 2 places (100.0); the contract keeps 99.99993.
    host = _build_source_host(parse_offline_collection(_sample("wordpress")))
    assert host is not None
    assert host.buffer_cache_read_requests == 5_026_219_966
    assert host.buffer_cache_reads_from_disk == 3_501
    assert host.buffer_cache_hit_ratio_pct == pytest.approx(99.99993, abs=1e-5)
    assert host.buffer_cache_hit_ratio_pct < 100.0
    # The MySQL script reports no host figures.
    assert host.cpu_count is None and host.physical_memory_gb is None


def test_postgresql_shared_buffer_counts_give_the_ratio() -> None:
    # discourse (PostgreSQL): the script maps pg_stat_statements' shared blocks to
    # the same keys: 24,886,463 reads, 2,680 from disk.
    host = _build_source_host(parse_offline_collection(_sample("discourse")))
    assert host is not None
    assert host.buffer_cache_hit_ratio_pct == pytest.approx(
        (24_886_463 - 2_680) / 24_886_463 * 100, abs=1e-4
    )


def test_sql_server_reports_the_host_cpu_and_memory() -> None:
    host = _build_source_host(parse_offline_collection(_sample("trailgear")))
    assert host is not None
    assert host.cpu_count == 8
    assert host.physical_memory_gb == pytest.approx(64.0)
    assert host.committed_memory_gb == pytest.approx(41.27)
    assert host.buffer_cache_hit_ratio_pct == pytest.approx(
        (918_442_310 - 4_118_224) / 918_442_310 * 100, abs=1e-4
    )


def test_no_reads_is_no_ratio_not_a_zero_percent_hit_ratio() -> None:
    # The parser turns missing counters into 0 reads and a 0.0 percent; a cost
    # estimate must not read that as "every read misses".
    host = _build_source_host(
        {"global_stats": {"buffer_pool_read_requests": 0, "buffer_pool_reads_from_disk": 0}}
    )
    assert host is None


def test_nothing_reported_is_no_source_host() -> None:
    assert _build_source_host({}) is None
    assert _build_source_host({"global_stats": {}, "os_stats": {}}) is None


def test_host_figures_alone_are_kept_without_a_ratio() -> None:
    host = _build_source_host({"os_stats": {"cpu_count": "4", "physical_memory_gb": "15.5"}})
    assert host is not None
    assert host.buffer_cache_hit_ratio_pct is None
    assert host.cpu_count == 4 and host.physical_memory_gb == pytest.approx(15.5)


@pytest.mark.parametrize(
    "os_stats",
    [{"cpu_count": 0}, {"cpu_count": "many"}, {"physical_memory_gb": -1}, {"cpu_count": None}],
)
def test_an_unusable_host_figure_is_dropped_not_fatal(os_stats: dict[str, Any]) -> None:
    assert _build_source_host({"os_stats": os_stats}) is None


def test_more_disk_reads_than_requests_is_not_a_ratio() -> None:
    host = _build_source_host(
        {"global_stats": {"buffer_pool_read_requests": 10, "buffer_pool_reads_from_disk": 20}}
    )
    assert host is None


# --- every raw-collection path carries it -------------------------------------------


@pytest.mark.parametrize("name", ["wordpress", "discourse", "trailgear"])
def test_run_assessment_collect_writes_source_host(tmp_path: Path, name: str) -> None:
    store = LocalArtifactStore(base_dir=str(tmp_path / "art"))
    job, db = run_assessment.phase_collect(str(_write_sample(name, tmp_path)), name, store)
    stored = store.read_json(f"{db}/{job}/collector/output.json")
    assert stored["contract_version"] == current_version(CollectorOutputContract)
    host = stored["metrics"]["source_host"]
    assert host["buffer_cache_hit_ratio_pct"] is not None
    assert 99.0 < host["buffer_cache_hit_ratio_pct"] < 100.0


def test_run_collect_writes_source_host(tmp_path: Path) -> None:
    src = _write_sample("trailgear", tmp_path)
    proc = subprocess.run(  # nosec B603 -- fixed interpreter plus this repo's own script args
        [
            sys.executable,
            "scripts/run_collect.py",
            "--file",
            str(src),
            "--db",
            "tg",
            "--job-id",
            "job00001",
            "--artifact-root",
            str(tmp_path / "art"),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    stored = json.loads(
        (tmp_path / "art" / "tg" / "job00001" / "collector" / "output.json").read_text()
    )
    assert stored["metrics"]["source_host"]["cpu_count"] == 8


def test_the_transform_ingest_writes_source_host() -> None:
    from src.atx_orchestrator import core

    class _Store:
        def __init__(self) -> None:
            self.data: dict[str, dict] = {}

        def write_json(self, path: str, data: dict) -> None:
            self.data[path] = data

    store = _Store()
    out = core.ingest_offline_collection(store, "job-1", "wordpress", _sample("wordpress"))
    assert out["metrics"]["source_host"]["buffer_cache_read_requests"] == 5_026_219_966


# --- older files still load -----------------------------------------------------------


def test_a_3_4_collector_output_without_source_host_still_loads() -> None:
    old: dict[str, Any] = {
        "contract_version": "3.4",
        "job_id": "j",
        "metadata": {
            "collection_timestamp": "2026-10-18T10:00:00Z",
            "collector_version": "1.0.0",
            "source_database": {"engine": "mysql", "version": "8.0.45", "hostname": "h"},
        },
        "database_schema": {
            "tables": [
                {
                    "table_id": "shop.orders",
                    "table_name": "orders",
                    "row_count": 1,
                    "columns": [{"column_name": "id", "data_type": "int", "nullable": False}],
                }
            ]
        },
        "queries": {"query_patterns": []},
        "metrics": {"performance_metrics": {}},
    }
    contract = CollectorOutputContract.model_validate(old)
    assert contract.metrics.source_host is None
    assert contract.contract_version == "3.4"


# --- the collector agent's own paths (review round 1) ---------------------------------


class _Checkpoints:
    """A checkpoint store that serves canned stage results (no AWS, no database)."""

    def __init__(self, stages: dict[str, Any]) -> None:
        self.stages = stages

    def load_or_run(self, name: str, fn: Any) -> Any:
        return self.stages[name] if name in self.stages else fn()


def _wordpress_contract_queries(contract: Any) -> list[Any]:
    return list(contract.queries.query_patterns)


def test_the_offline_collector_keeps_the_ratio_once_and_unrounded() -> None:
    """``_collect_offline`` (every engine's offline mode): the instance ratio is in
    ``metrics.source_host`` at full precision and on no query, so the DocumentDB
    estimate names the collection, not "each query's own", as its basis."""
    from src.agents.collector import mysql_collector
    from src.contracts.collector_input import CollectorInput
    from src.tools.analysis.documentdb_analysis_tools import estimate_documentdb_costs

    inp = CollectorInput.model_validate(
        {
            "job_id": "j",
            "engine": "mysql",
            "cluster_endpoint": "offline",
            "port": 3306,
            "database_name": "wordpress",
            "mode": "offline",
            "offline_config": {"s3_bucket": "b", "s3_key": "k"},
        }
    )
    parsed = parse_offline_collection(_sample("wordpress"))
    ckpt = _Checkpoints({"offline_schema": parsed, "aws_metrics": {}})
    contract = mysql_collector._collect_offline(inp, ckpt)
    host = contract.metrics.source_host
    assert host is not None
    assert host.buffer_cache_hit_ratio_pct == pytest.approx(99.99993, abs=1e-5)
    assert all(p.cache_hit_ratio_pct is None for p in _wordpress_contract_queries(contract))
    est = estimate_documentdb_costs(contract.model_dump(mode="json"))
    assert est.cost_components["read_io_hit_ratio_basis"] == "collection"


def test_the_offline_collector_keeps_the_host_memory_the_bound_scales_by() -> None:
    """``_collect_offline`` with the SQL Server sample: the host's physical memory
    reaches ``metrics.source_host``, and the DocumentDB estimate scales the ratio by
    the 16 GiB target over it (decision on #1222)."""
    from src.agents.collector import mysql_collector
    from src.contracts.collector_input import CollectorInput
    from src.tools.analysis.documentdb_analysis_tools import estimate_documentdb_costs

    inp = CollectorInput.model_validate(
        {
            "job_id": "j",
            "engine": "sqlserver",
            "cluster_endpoint": "offline",
            "port": 1433,
            "database_name": "trailgear",
            "mode": "offline",
            "offline_config": {"s3_bucket": "b", "s3_key": "k"},
        }
    )
    parsed = parse_offline_collection(_sample("trailgear"))
    ckpt = _Checkpoints({"offline_schema": parsed, "aws_metrics": {}})
    contract = mysql_collector._collect_offline(inp, ckpt)
    host = contract.metrics.source_host
    assert host is not None
    assert host.physical_memory_gb == 64.0
    assert host.buffer_cache_hit_ratio_pct == pytest.approx(99.551608, abs=1e-6)
    components = estimate_documentdb_costs(contract.model_dump(mode="json")).cost_components
    assert components["source_memory_gb"] == 64.0
    assert components["collection_hit_ratio_billed_pct"] == pytest.approx(24.887902, abs=1e-6)


def test_the_live_collector_keeps_the_ratio_once_and_unrounded() -> None:
    """Live mode reads the database's own global stats (same keys as the offline
    parser's): the ratio goes to ``metrics.source_host``, never onto each query."""
    from unittest.mock import MagicMock, patch

    from src.agents.collector import mysql_collector
    from src.contracts.collector_input import CollectorInput

    inp = CollectorInput.model_validate(
        {
            "job_id": "j",
            "engine": "mysql",
            "cluster_endpoint": "db.example",
            "port": 3306,
            "database_name": "wordpress",
            "mode": "live",
            "live_config": {"secret_arn": "s", "automation_instance_id": "i"},
        }
    )
    parsed = parse_offline_collection(_sample("wordpress"))
    ckpt = _Checkpoints(
        {
            "metadata": {"version": "8.0.45", "db_size_gb": 0.135},
            "schema": {"tables": parsed["tables"], "global_stats": parsed["global_stats"]},
            "queries": parsed["queries"],
            "aws_metrics": {},
        }
    )
    with (
        patch.object(mysql_collector, "_build_cred_mgr", return_value=MagicMock()),
        patch.object(mysql_collector, "SSMExecutor", MagicMock()),
        patch.object(mysql_collector, "MySQLRemoteCollector", MagicMock()),
    ):
        contract = mysql_collector._collect_live(inp, ckpt)
    host = contract.metrics.source_host
    assert host is not None
    assert host.buffer_cache_hit_ratio_pct == pytest.approx(99.99993, abs=1e-5)
    assert all(p.cache_hit_ratio_pct is None for p in _wordpress_contract_queries(contract))


def test_the_instance_ratio_is_not_copied_onto_queries() -> None:
    from src.agents.collector.mysql_collector import _enrich_patterns_from_pi_and_cw
    from src.contracts.collector_output import Queries, QueryPattern

    own = QueryPattern(
        query_id="a",
        query_text="SELECT 1",
        frequency_per_hour=1,
        tables_accessed=["t"],
        cache_hit_ratio_pct=42.0,
    )
    plain = QueryPattern(
        query_id="b", query_text="SELECT 2", frequency_per_hour=1, tables_accessed=["t"]
    )
    queries = Queries(query_patterns=[own, plain])
    _enrich_patterns_from_pi_and_cw(queries, {"cache_hit_ratio_pct": 99.99}, {})
    assert own.cache_hit_ratio_pct == 42.0
    assert plain.cache_hit_ratio_pct is None


def test_a_reported_rate_without_counts_is_the_ratio() -> None:
    # Performance Insights reports the buffer-pool hit rate itself.
    host = _build_source_host({"global_stats": {"cache_hit_ratio_pct": 99.5}})
    assert host is not None and host.buffer_cache_hit_ratio_pct == 99.5
    # The parser's 0.0 for zero reads is not a reported rate.
    assert (
        _build_source_host(
            {
                "global_stats": {
                    "cache_hit_ratio_pct": 0.0,
                    "buffer_pool_read_requests": 0,
                    "buffer_pool_reads_from_disk": 0,
                }
            }
        )
        is None
    )
