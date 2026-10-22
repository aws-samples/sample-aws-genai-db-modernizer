"""Every JSON file a run writes is a contract or a declared internal file (#1192).

ADR-008 Decision 1 (amendment of 2026-10-20): a JSON artifact under a job is one of

- a registered contract (``_ARTIFACT_CONTRACTS`` and the schema outputs in
  ``src/shared/contract_versioning.py``): versioned and written through its model;
- model I/O (``MODEL_IO_PATTERNS``): a model session's own request, input or answer;
- a declared internal artifact (``INTERNAL_ARTIFACTS``), each with its reason.

Two scans find the files, and each fails on one in none of the lists:

- the write scan of ``test_contract_writes_go_through_model.py`` (every
  ``.write_json`` in ``src/`` and ``scripts/`` whose file name it can render);
- real runs: ``run_assessment.py --all -y --llm-mode none`` on each bundled sample,
  and a wordpress run in ``--llm-mode external`` with this test as the model (the
  reality check, the DynamoDB group drafts and their PE reviews, the Aurora MySQL and
  ElastiCache designs, the executive summary), through the scripts a headless
  ``/modernize`` calls. No test calls a model.

An entry of any of the three lists that neither scan produces is stale and fails
too, unless ``NOT_PRODUCED_HERE`` names who writes it.
Blind spots: the scan sees only ``.write_json`` calls, so a JSON file written with
``write_text``, ``write_bytes`` or ``json.dump`` (``collector/sql_shadow.json`` is),
or one whose file name the scan cannot render (a constant from another module, a
caller's parameter), is checked only when a run writes it; and a run covers only the
paths it takes (no API, AWS Transform or load-test run here).
"""

from __future__ import annotations

import ast
import functools
import json
import os
import re
import subprocess  # nosec B404 -- runs this repo's own scripts with fixed argv
import sys
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest

from src.shared import contract_versioning as versioning
from src.shared.catalog import engine_ids
from src.storage.local_store import LocalArtifactStore
from tests.contract.test_contract_writes_go_through_model import WRITES, _contract_of
from tests.contract.test_reads_check_contract_version import _PLACEHOLDER
from tests.shared_runs import cached_run

REPO = Path(__file__).resolve().parents[2]
# Every bundled sample (``docs/examples/<name>/<name>.zip``).
SAMPLES = tuple(
    sorted(
        z.parent.name
        for z in (REPO / "docs" / "examples").glob("*/*.zip")
        if z.stem == z.parent.name
    )
)
TEMPLATE = json.loads(
    (
        REPO / "docs/examples/wordpress/load-test-results/schema-dynamodb/v2/schema_output.json"
    ).read_text()
)


def kind(path: str) -> str | None:
    """``contract``, ``model_io`` or ``internal`` for a declared JSON path, else ``None``."""
    if versioning.is_model_io(path):
        return "model_io"
    if _contract_of(path) is not None:
        return "contract"
    if versioning.is_internal_artifact(path):
        return "internal"
    return None


def undeclared(paths: list[str]) -> list[str]:
    return [p for p in paths if kind(p) is None]


# --- the write scan ----------------------------------------------------------------

# A shape whose file name the renderer worked out (``design_trace_group_1.json``); a
# name from a variable renders as the placeholder alone (``.../1``) and is left to the runs.
_NAMED_JSON = re.compile(r"(?:^|/)[^/]*[A-Za-z_-][^/]*\.json$")


# Writes whose path is a module constant the renderer does not resolve: (file, the
# constant's name). Its value is read from the file, so a renamed constant fails here.
CONSTANT_PATHS: tuple[tuple[str, str], ...] = (
    ("src/api/services/local_execution.py", "_STATUS_MARKER"),
    ("src/api/stored_input_redaction.py", "MARKER"),
)


def _constant(file: str, name: str) -> str:
    tree = ast.parse((REPO / file).read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and [getattr(t, "id", None) for t in node.targets] == [
            name
        ]:
            value = ast.literal_eval(node.value)
            assert isinstance(value, str)
            return f"db/job/{value}" if not value.startswith(".") else value
    raise AssertionError(f"{file} defines no {name}")


def _written_shapes() -> dict[str, str]:
    """Every rendered ``.json`` write path in ``src/`` and ``scripts/`` -> one writer."""
    shapes: dict[str, str] = {}
    for write in WRITES:
        for shape in write.shapes:
            if _NAMED_JSON.search(shape):
                shapes.setdefault(shape, write.where())
    for file, name in CONSTANT_PATHS:
        shapes.setdefault(_constant(file, name), f"{file} ({name})")
    return shapes


SHAPES = _written_shapes()


def test_the_scan_finds_internal_writes() -> None:
    assert any(kind(s) == "internal" for s in SHAPES)
    assert any(kind(s) == "contract" for s in SHAPES)


def test_every_written_json_path_is_declared() -> None:
    offenders = {s: where for s, where in SHAPES.items() if kind(s) is None}
    assert not offenders, (
        "These writes store a JSON file that is neither a registered contract, model "
        "I/O nor a declared internal artifact (ADR-008 Decision 1, #1192). Make it a "
        "contract (register it in _ARTIFACT_CONTRACTS and write it through its model), "
        "or add it to INTERNAL_ARTIFACTS in src/shared/contract_versioning.py with the "
        "reason:\n" + "\n".join(f"  {s}  <- {where}" for s, where in sorted(offenders.items()))
    )


# --- the runs ----------------------------------------------------------------------


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("MODERNIZER_CI_SANDBOX", None)
    env.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    for k in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        env.pop(k, None)  # no AWS access is needed, and none may be used
    return env


def _script(cwd: Path, script: str, *args: str) -> dict:
    """Run ``scripts/<script>`` from ``cwd`` (its state file stays there); the last
    JSON status line it prints."""
    proc = subprocess.run(  # nosec B603 -- fixed interpreter plus this repo's own script
        [sys.executable, str(REPO / "scripts" / script), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=_env(),
        timeout=600,
    )
    parsed = [json.loads(ln) for ln in proc.stdout.splitlines() if ln.startswith("{")]
    # The last line may be only the progress-log pointer (``{"log": ...}``).
    status: list[dict] = [d for d in parsed if not set(d) <= {"log", "log_offset", "log_lines"}]
    assert proc.returncode == 0 and status, (
        f"{script} {' '.join(args)} failed (exit {proc.returncode})\n"
        f"{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}"
    )
    return status[-1]


def _collection(sample: str, directory: Path) -> Path:
    with zipfile.ZipFile(REPO / "docs" / "examples" / sample / f"{sample}.zip") as z:
        z.extractall(directory / "input")
    return directory / "input" / f"{sample}-collection.json"


def _deterministic_run(sample: str, directory: Path) -> None:
    _script(
        directory,
        "run_assessment.py",
        "--file",
        str(_collection(sample, directory)),
        "--db",
        sample,
        "--all",
        "-y",
        "--llm-mode",
        "none",
        "--artifact-root",
        str(directory / "artifacts"),
    )


def _write(path: Path, data: dict) -> None:
    """The model's Write tool call (it creates the parent directory)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def _draft(queries: list[dict]) -> dict:
    """A DynamoDB group draft: the committed wordpress design's first table, once per
    source table the group reads, and one access pattern per query."""
    from tests.unit.orchestrator.test_pe_flag_fixture_wordpress_external import _draft_from

    return _draft_from(queries)


class _Capture:
    """A store that keeps writes in memory and reads from ``store``."""

    def __init__(self, store: LocalArtifactStore) -> None:
        self.store = store
        self.written: dict[str, dict] = {}

    def write_json(self, key: str, data: dict) -> None:
        self.written[key] = data

    def __getattr__(self, name: str):
        return getattr(self.store, name)


def _elasticache_answer(store: LocalArtifactStore, db: str, job: str, version: int) -> dict:
    """The designed e2e job's ElastiCache design, as the model's answer."""
    from tests.e2e.designed_run import write_elasticache_design

    capture = _Capture(store)
    write_elasticache_design(capture, db, job, version)  # type: ignore[arg-type]
    [answer] = capture.written.values()
    return answer


def _mocked_model_run(directory: Path) -> None:
    """wordpress through the headless scripts, with this test answering for the model."""
    root = directory / "artifacts"
    db = "wordpress"
    first = _script(
        directory,
        "run_assessment.py",
        "--file",
        str(_collection(db, directory)),
        "--db",
        db,
        "--all",
        "-y",
        "--llm-mode",
        "external",
        "--artifact-root",
        str(root),
    )
    assert first["status"] == "awaiting_llm", first
    job = Path(first["llm_request"]).parts[-3]
    common = ["--job-id", job, "--db", db, "--artifact-root", str(root)]
    job_dir = root / db / job

    # Reality check: every moved query fits its target.
    _write(
        job_dir / "llm_responses" / "reality_check.json",
        {
            "consolidation_corrections": [],
            "executive_summary": "Your workload runs on Amazon DynamoDB and Aurora MySQL.",
        },
    )
    done = _script(directory, "run_assessment.py", *common, "--resume-reality-check")
    assert done["status"] == "complete", done

    # DynamoDB: split, one draft per group, a PE review per group, merge.
    split = _script(directory, "run_schema_design.py", *common, "--engine", "dynamodb", "--split")
    for group in split["groups"]:
        group_input = json.loads(Path(group["input_file"]).read_text())
        _write(
            Path(group["draft"]),
            _draft(group_input["collector_output"]["queries"]["query_patterns"]),
        )
    reviews = _script(directory, "run_pe_review.py", *common)
    for review in reviews["reviews"]:
        _write(
            Path(review["response"]),
            {"verdict": "APPROVED", "summary": "The design serves every access pattern."},
        )
        group = [] if review["group"] is None else ["--group", str(review["group"])]
        _script(
            directory,
            "run_pe_review.py",
            *common,
            "--engine",
            review["engine"],
            *group,
            "--finalize",
        )
    merged = _script(directory, "run_schema_design.py", *common, "--engine", "dynamodb", "--merge")
    assert merged["status"] == "complete", merged

    # Aurora MySQL (an empty design delta) and ElastiCache, each requested and finalized.
    store = LocalArtifactStore(base_dir=str(root))
    for engine in ("aurora_mysql", "elasticache"):
        request = _script(directory, "run_schema_design.py", *common, "--engine", engine)
        assert request["status"] == "awaiting_llm", request
        version = int(request["assignment_version"])
        if engine == "aurora_mysql":
            answer: dict = {"delta_version": "1.0"}
        else:
            answer = _elasticache_answer(store, db, job, version)
        _write(job_dir / "llm_responses" / f"schema_design_{engine}.json", answer)
        final = _script(
            directory, "run_schema_design.py", *common, "--engine", engine, "--finalize"
        )
        assert final["status"] == "complete", final

    _script(directory, "run_post_schema_routing.py", *common)

    # Synthesis: the executive summary, then the deliverables.
    synthesis = _script(directory, "run_synthesis.py", *common, "--llm-mode", "external")
    assert synthesis["status"] == "awaiting_llm", synthesis
    _write(
        root / synthesis["llm_response"],  # relative to the artifact root, or absolute
        {"executive_summary": "Your workload moves to Amazon DynamoDB and Aurora MySQL."},
    )
    _script(directory, "run_synthesis.py", *common, "--finalize")
    report = _script(directory, "run_report.py", *common)
    assert report["status"] == "complete", report


RUNS: dict[str, Callable[[Path], None]] = {
    **{f"none-{s}": functools.partial(_deterministic_run, s) for s in SAMPLES},
    "mocked-model-wordpress": _mocked_model_run,
}


def _run_files(tmp_path_factory: pytest.TempPathFactory, name: str) -> list[str]:
    """``name``'s JSON files, relative to its artifact root. Each run is built once per
    session (``tests/shared_runs.py``), by whichever xdist worker asks first."""
    root = cached_run(tmp_path_factory, f"declared-artifacts-{name}", RUNS[name]) / "artifacts"
    return sorted(str(p.relative_to(root)) for p in root.rglob("*.json"))


@pytest.mark.passes_on_main  # a check of the sample discovery
def test_every_bundled_sample_is_run() -> None:
    assert {"wordpress", "discourse", "trailgear"} <= set(SAMPLES)


@pytest.mark.parametrize("name", sorted(RUNS))
def test_every_json_file_a_run_writes_is_declared(tmp_path_factory, name: str) -> None:
    files = _run_files(tmp_path_factory, name)
    assert any(f.endswith("/report.json") for f in files), files
    offenders = undeclared(files)
    assert not offenders, (
        f"The {name} run wrote JSON files that are neither a registered contract, model "
        "I/O nor a declared internal artifact (ADR-008 Decision 1, #1192). Make each a "
        "contract or add it to INTERNAL_ARTIFACTS with the reason:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.passes_on_main  # a check of the run harness, not of the change
def test_the_mocked_model_run_wrote_model_io_and_designs(tmp_path_factory) -> None:
    """The model's files and the designs are there, so the run took the model's path."""
    files = _run_files(tmp_path_factory, "mocked-model-wordpress")
    for suffix in (
        "/reality-check/llm_input.json",
        "/llm_responses/reality_check.json",
        "/schema-dynamodb/v2/schema_output.json",
        "/schema-aurora_mysql/v2/schema_output.json",
        "/schema-elasticache/v2/schema_output.json",
        "/synthesis/v2/llm_response.json",
    ):
        assert any(f.endswith(suffix) for f in files), suffix


# Entries of the other two lists that neither the scan nor a run here produces, with
# who writes them. Kept short; an entry here that a scan or run does produce is stale too.
NOT_PRODUCED_HERE: dict[str, str] = {
    r"load-test-[^/]+/v\d+/results/(?!summary\.json)[^/]+\.json": (
        "a load test's per-pattern result, written by the load test runner (no load test "
        "runs here; its path is a caller's parameter the scan cannot render)"
    ),
}


def _entries() -> list[tuple[str, str, re.Pattern[str]]]:
    """(list, pattern, compiled) for every entry of the three lists. The schema outputs
    are one entry, ``_SCHEMA_OUTPUT``, for every engine that has a design contract."""
    contracts = [raw for raw, _ in versioning._ARTIFACT_CONTRACTS]
    entries = [
        ("registry", raw, compiled)
        for raw, (compiled, _target) in zip(contracts, versioning._registry(), strict=False)
    ]
    engines = "|".join(
        re.escape(raw.split("/", 1)[0].removeprefix("schema-"))
        for raw, _ in versioning._schema_output_targets()
    )
    template = versioning._SCHEMA_OUTPUT
    entries.append(
        ("registry", template, re.compile(rf"(?:^|/){template.format(engine=f'(?:{engines})')}$"))
    )
    entries += [
        ("model I/O", raw, re.compile(rf"(?:^|/){raw}$")) for raw in versioning.MODEL_IO_PATTERNS
    ]
    entries += [("internal", raw, compiled) for raw, compiled in versioning._internal()]
    return entries


def _candidates(path: str) -> list[str]:
    """``path``, and a scan shape with its interpolated engine tried as each engine."""
    return [path] + [
        re.sub(rf"(?<=-){re.escape(_PLACEHOLDER)}(?=/)", engine, path)
        for engine in sorted(engine_ids())
    ]


def test_no_stale_entries_in_any_list(tmp_path_factory) -> None:
    """Every entry of the registry, model I/O and the internal list matches a path the
    scan renders or a run writes, unless ``NOT_PRODUCED_HERE`` names who writes it."""
    files = [f for name in sorted(RUNS) for f in _run_files(tmp_path_factory, name)]
    produced = [c for p in [*SHAPES, *files] for c in _candidates(p)]
    unmatched = {
        raw: kind_
        for kind_, raw, compiled in _entries()
        if not any(compiled.search(p) for p in produced)
    }
    stale = {raw: k for raw, k in unmatched.items() if raw not in NOT_PRODUCED_HERE}
    assert not stale, f"entries no write or run produces (list them or drop them): {stale}"
    assert not set(NOT_PRODUCED_HERE) - set(unmatched), (
        "NOT_PRODUCED_HERE entries a scan or run now produces: "
        f"{set(NOT_PRODUCED_HERE) - set(unmatched)}"
    )


# --- the lists themselves ----------------------------------------------------------


def test_every_internal_entry_has_a_one_line_reason() -> None:
    for pattern, reason in versioning.INTERNAL_ARTIFACTS:
        assert reason.strip() and "\n" not in reason, pattern


@pytest.mark.passes_on_main
def test_no_path_is_both_a_contract_and_internal() -> None:
    """An internal pattern never covers a registered contract file (a typed file
    leaves INTERNAL_ARTIFACTS when it is registered)."""
    both = [s for s in SHAPES if _contract_of(s) is not None and versioning.is_internal_artifact(s)]
    assert not both, both
    trace = "db/job/analysis-dynamodb/decision-trace.json"
    assert _contract_of(trace) is not None
    assert not versioning.is_internal_artifact(trace)
    assert versioning.is_internal_artifact("db/job/analysis-aurora_mysql/decision-trace.json")


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("db/job/assignment/v2/assignment.json", "contract"),
        ("db/job/llm_responses/reality_check.json", "model_io"),
        ("db/job/collector/sql_shadow.json", "internal"),
        ("db/job/decisions/assignment_approval.json", "contract"),
        ("db/job/collector/notes.json", None),
        ("db/job/decisions/nested/x.json", None),
        ("db/job/decisions/other_decision.json", "contract"),
        ("db/job/_meta.json", "internal"),
        ("db/job/_status.json", "internal"),
        ("db/job/collector/error.json", "internal"),
        ("db/job/dynamodb/error.json", "internal"),
        ("db/job/schema-dynamodb/v1/_meta.json", None),
        ("db/job/synthesis/v2/_status.json", None),
        ("db/job/deliverables/error.json", None),
        ("_meta.json", None),
        ("db/job/analysis-dynamodb/er-diagram.json.bak", None),
    ],
)
def test_kind_tells_the_three_lists_apart(path: str, expected: str | None) -> None:
    assert kind(path) == expected


def test_an_undeclared_file_is_reported() -> None:
    files = ["db/job/collector/output.json", "db/job/collector/new_record.json"]
    assert undeclared(files) == ["db/job/collector/new_record.json"]
