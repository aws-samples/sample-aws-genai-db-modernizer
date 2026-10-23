"""build_docs_sample.py copies deliverables into the built help site (#364).

By default it renders the recorded model run committed under
docs/examples/wordpress/sample-run/ with this checkout's renderers (#1260); the
first group of tests renders that committed run for real (run_report.py's
``run``, in process: no model, no subprocess) and checks it renders every
deliverable with no error and no warning, that a report missing figures gets
the stored ones back from the facts backfill (ADR-032 Decision 4), and that the
committed files carry no private detail.

For the ``--deterministic`` fallback, run_pipeline is monkeypatched -- it shells
out to this repo's own scripts/run_*.py as subprocesses, which is appropriate
for the e2e suite but too slow and heavy for a unit test. Those tests only check
what build_sample does with the paths it gets back: it must never touch the
themed `sample/index.html` MkDocs already rendered from docs/sample.md, it must
fail fast if that file is missing, and it must copy each recognized file of the
run's deliverables folder under the same name.

This suite must pass with only the `dev` extra installed (that is what
`ci/test.sh` installs, and it runs with `--fail-on-skip`), so it never
relies on the `markdown` package actually being present or absent. Both
of its code paths are forced via `sys.modules`: a fake module stands in
for a real one (`_install_fake_markdown`), and `sys.modules["markdown"]
= None` forces the `ImportError` branch `_render_engineering_html`
already handles, the same trick the standard library itself uses to
simulate a missing optional dependency.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
import shutil
import string
import sys
import types
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from scripts import build_docs_sample
from src.report.deliverables import FOLDER_FILENAMES, RETIRED_FOLDER_FILENAMES
from src.report.executive_report import collected_identifiers
from src.report.facts import report_facts


@dataclass
class _FakePipelineResult:
    job_id: str
    steps: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def report(self) -> dict[str, Any]:
        return self.steps["report"]


def _make_job_paths(tmp_path: Path) -> list[str]:
    """The run's deliverables folder, as run_report.py reports it."""
    folder = tmp_path / "artifacts" / "wordpress" / "docs-sample" / "deliverables"
    folder.mkdir(parents=True)

    files = {
        "engineering-report.md": b"# Engineering report\n\nSome **content**.\n",
        "executive-report.pdf": b"%PDF-1.4 fake pdf bytes",
        "assessment-data.json": b"{}",
        "index.md": b"# index",
        # Not a documented deliverable: never published even if listed.
        "llm_input.json": b"{}",
        # A retired deliverable an older round left behind (#987): never published.
        **{name: b"<html>old</html>" for name in RETIRED_FOLDER_FILENAMES},
    }
    for name, content in files.items():
        (folder / name).write_bytes(content)
    return [str(folder / name) for name in files]


@pytest.fixture
def site_dir(tmp_path: Path) -> Path:
    site = tmp_path / "site"
    sample = site / "sample"
    sample.mkdir(parents=True)
    (sample / "index.html").write_text("<html>themed sample page</html>", encoding="utf-8")
    return site


def _patch_run_pipeline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    paths = _make_job_paths(tmp_path)

    def _fake_run_pipeline(sample: str, artifact_root: Path, job_id: str) -> _FakePipelineResult:
        return _FakePipelineResult(job_id=job_id, steps={"report": {"deliverables": paths}})

    monkeypatch.setattr(build_docs_sample, "run_pipeline", _fake_run_pipeline)


def _install_fake_markdown(monkeypatch: pytest.MonkeyPatch, rendered_html: str) -> None:
    """Stand in for the real `markdown` package, present or not.

    `_render_engineering_html` does `import markdown; markdown.markdown(...)`,
    so the fake only needs that one attribute.
    """
    fake = types.ModuleType("markdown")
    fake.markdown = lambda text, extensions=None: rendered_html  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "markdown", fake)


def test_build_sample_does_not_touch_themed_index(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, site_dir: Path
) -> None:
    _patch_run_pipeline(monkeypatch, tmp_path)
    index_path = site_dir / "sample" / "index.html"
    before = index_path.read_text(encoding="utf-8")

    build_docs_sample.build_sample(
        site_dir, sample="wordpress", job_id="docs-sample", deterministic=True
    )

    assert index_path.read_text(encoding="utf-8") == before


def test_build_sample_copies_deliverables_under_stable_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, site_dir: Path
) -> None:
    _patch_run_pipeline(monkeypatch, tmp_path)

    result = build_docs_sample.build_sample(
        site_dir, sample="wordpress", job_id="docs-sample", deterministic=True
    )

    dest = site_dir / "sample"
    assert result["status"] == "complete"
    assert (dest / "engineering-report.md").exists()
    assert (dest / "executive-report.pdf").read_bytes() == b"%PDF-1.4 fake pdf bytes"
    assert (dest / "assessment-data.json").exists()
    assert (dest / "index.md").exists()
    # Only the documented set: model I/O and retired reports never reach the site.
    assert not (dest / "llm_input.json").exists()
    for name in RETIRED_FOLDER_FILENAMES:
        assert not (dest / name).exists(), name
    for p in result["files"]:
        assert Path(p).name != "index.html"


def test_build_sample_renders_engineering_html_when_markdown_available(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, site_dir: Path
) -> None:
    _install_fake_markdown(monkeypatch, rendered_html="<p>FAKE RENDERED</p>")
    _patch_run_pipeline(monkeypatch, tmp_path)

    result = build_docs_sample.build_sample(
        site_dir, sample="wordpress", job_id="docs-sample", deterministic=True
    )

    rendered = (site_dir / "sample" / "engineering-report.html").read_text(encoding="utf-8")
    assert "<p>FAKE RENDERED</p>" in rendered
    assert 'href="./"' in rendered  # links back to the themed sample page, not a generated one
    # Landmarks for assistive tech: the links in a header, the report in one <main>.
    assert rendered.count("<main>") == 1 and "<header>" in rendered
    assert any(Path(p).name == "engineering-report.html" for p in result["files"])


def test_build_sample_skips_engineering_html_without_markdown_package(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, site_dir: Path
) -> None:
    # Setting sys.modules["markdown"] = None forces `import markdown` to
    # raise ImportError, regardless of whether the real package happens to
    # be installed in the environment running this test.
    monkeypatch.setitem(sys.modules, "markdown", None)
    _patch_run_pipeline(monkeypatch, tmp_path)

    result = build_docs_sample.build_sample(
        site_dir, sample="wordpress", job_id="docs-sample", deterministic=True
    )

    assert not (site_dir / "sample" / "engineering-report.html").exists()
    assert (site_dir / "sample" / "engineering-report.md").exists()
    assert all(Path(p).name != "engineering-report.html" for p in result["files"])


def test_build_sample_fails_fast_without_mkdocs_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_run_pipeline(monkeypatch, tmp_path)
    site = tmp_path / "unbuilt-site"
    site.mkdir()

    with pytest.raises(FileNotFoundError, match="mkdocs build"):
        build_docs_sample.build_sample(
            site, sample="wordpress", job_id="docs-sample", deterministic=True
        )


# ---------------------------------------------------------------------------
# The recorded model run (#1260)
# ---------------------------------------------------------------------------

RUN_DIR = build_docs_sample.SAMPLE_RUN_DIR


def _committed_report() -> dict[str, Any]:
    report: dict[str, Any] = json.loads(
        build_docs_sample.sample_run_report(RUN_DIR).read_text(encoding="utf-8")
    )
    return report


def test_build_sample_renders_the_committed_model_run_by_default(
    monkeypatch: pytest.MonkeyPatch, site_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def _no_pipeline(*_a: Any, **_kw: Any) -> None:
        raise AssertionError("the default path renders the recorded run, never the pipeline")

    monkeypatch.setattr(build_docs_sample, "run_pipeline", _no_pipeline)
    caplog.set_level(logging.WARNING)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        result = build_docs_sample.build_sample(site_dir)

    assert caplog.records == [], [r.getMessage() for r in caplog.records]
    report = _committed_report()
    assert result["source"] == "sample-run"
    assert result["job_id"] == report["job_id"]
    dest = site_dir / "sample"
    assert {Path(p).name for p in result["files"]} >= set(FOLDER_FILENAMES)
    assert (dest / "executive-report.pdf").read_bytes().startswith(b"%PDF")
    # A model run: the model-written summary and the per-engine schemas are in the
    # engineering report, which a deterministic run (no Schema Design) cannot show.
    assert report["summary_source"] == "llm"
    engineering = (dest / "engineering-report.md").read_text(encoding="utf-8")
    assert report["summary"].split(",")[0] in engineering
    assert set(report["schema_designs"]) == {"aurora_mysql", "dynamodb", "elasticache"}
    assert "## Target schemas by engine" in engineering
    data = json.loads((dest / "assessment-data.json").read_text(encoding="utf-8"))
    assert data["schema_designs"] == report["schema_designs"]


def test_committed_run_index_names_its_assignment_version(site_dir: Path) -> None:
    build_docs_sample.build_sample(site_dir)
    version = build_docs_sample.sample_run_report(RUN_DIR).parent.name  # "v2"
    index = (site_dir / "sample" / "index.md").read_text(encoding="utf-8")
    assert f"Assignment version: {version}" in index
    assert f"Job: {_committed_report()['job_id']}" in index


def test_committed_run_renders_with_no_render_error(tmp_path: Path) -> None:
    result = build_docs_sample.render_sample_run(RUN_DIR, tmp_path / "artifacts")
    assert result["status"] == "complete"
    assert result["errors"] == []


def test_a_partial_render_fails_the_build(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def _partial(*_a: Any, **_kw: Any) -> dict[str, Any]:
        return {"status": "partial", "errors": ["executive-report: ValueError: boom"]}

    monkeypatch.setattr(build_docs_sample, "run_report", _partial)
    with pytest.raises(RuntimeError, match="did not render completely"):
        build_docs_sample.render_sample_run(RUN_DIR, tmp_path / "artifacts")


def test_a_sample_with_no_recorded_run_needs_the_deterministic_flag(site_dir: Path) -> None:
    with pytest.raises(ValueError, match="--deterministic"):
        build_docs_sample.build_sample(site_dir, sample="discourse")


# Each figure the facts reader fills in for an older report (ADR-032 Decision 4),
# removed the way an older synthesis would not have written it.
_BACKFILLED = {
    "access_pattern_totals": lambda f: f.pop("access_pattern_totals"),
    "moving_query_count": lambda f: f.pop("moving_query_count"),
    "query_pattern_counts": lambda f: f.pop("query_pattern_counts"),
    "resolved_risks_by_severity": lambda f: f.pop("resolved_risks_by_severity"),
    "retired_in_use_engines": lambda f: f.pop("retired_in_use_engines"),
    "unservable kinds": lambda f: f["unservable"].pop("no_fit_query_count"),
    "map split": lambda f: f["table_basis"].pop("mapped_tables_by_move"),
    "kept in sync": lambda f: [e.pop("kept_in_sync_tables") for e in f["engines"]],
    "shares replica risk": lambda f: [
        r.pop("shares_replica_risk") for r in f["table_sources"]["replicated_tables"]
    ],
    "heterogeneous note": lambda f: [w.pop("heterogeneous_note") for w in f["waves"]],
    "trace groups": lambda f: f["query_trace"].update(trace_groups=[]),
}


@pytest.mark.parametrize("figure", sorted(_BACKFILLED))
def test_backfill_gives_an_older_report_the_figures_this_run_stored(figure: str) -> None:
    report = _committed_report()
    older = copy.deepcopy(report)
    _BACKFILLED[figure](older["facts"])
    assert older["facts"] != report["facts"]  # the figure really was removed
    # The recorded run's own block, read through the same reader: a figure added after
    # the run was recorded (#1284's waves[].moved_by_later_waves, #212's
    # table_owner_lines, #1195's cost_assumptions) is filled in for both, every figure
    # the run stored is read as stored. #212 is the one deliberate exception: the Migration map split counts each
    # row under its owner now, never on the cache layer, so it is read anew (same total).
    assert report_facts(older) == report_facts(report)
    read = copy.deepcopy(report_facts(report))
    for row in read["waves"]:
        row.pop("moved_by_later_waves")
    assert read.pop("table_owner_lines")["table_count"] == len(report["table_mappings"])
    split = read["table_basis"].pop("mapped_tables_by_move")
    stored = copy.deepcopy(report["facts"])
    stored_split = stored["table_basis"].pop("mapped_tables_by_move")
    assert sum(split.values()) == sum(stored_split.values())
    assert "cache_layer" in stored_split and "cache_layer" not in split
    # The recorded sample's 22 rows: the one table the cache layer had joins the 3
    # that stay on the source engine (its owner is Aurora MySQL).
    assert split == {"migrate": 15, "kept_in_sync": 3, "stays": 4}
    # And the by-role count, by owner the same way (the cache layer owns none).
    by_role = read["table_basis"].pop("mapped_tables_by_role")
    stored["table_basis"].pop("mapped_tables_by_role")
    assert by_role == {"Migration target": 15, "Retained": 7}
    # #1329: filled in for a block recorded before the count (no scope, so 0).
    if "no_engine_in_scope_query_count" not in stored["unservable"]:
        assert read["unservable"].pop("no_engine_in_scope_query_count") == 0
        assert read["unservable"].pop("no_engine_in_scope_closest_engines") == []
    # #1329: and the headline total with the queries that have no engine (none here).
    if "query_total_count" not in stored:
        assert read.pop("no_engine_in_scope_query_count") == 0
        assert read.pop("query_total_count") == stored["in_scope_query_count"]
    # #1195: facts.cost_assumptions, also added after the run was recorded.
    read.pop("cost_assumptions")
    # #1252: facts.cache_layer, likewise added after the run was recorded.
    if "cache_layer" not in stored:
        cache = read.pop("cache_layer")
        # The recorded run predates the stored candidates: its dropped reads are counted back in.
        overlay = report["cache_overlay"]
        dropped = len(overlay["dropped_query_ids"])
        assert dropped > 0  # the recorded run did drop some, so this backfill is exercised
        assert cache["candidate_read_count"] == overlay["query_count"] + dropped
        assert cache["owner_only_read_count"] == dropped
        assert cache["candidate_call_share_percent"] is None
    assert read == stored


def test_committed_run_is_only_what_the_renderers_read() -> None:
    files = sorted(p.relative_to(RUN_DIR).as_posix() for p in RUN_DIR.rglob("*") if p.is_file())
    report_rel = build_docs_sample.sample_run_report(RUN_DIR).relative_to(RUN_DIR).as_posix()
    assert files == sorted(["README.md", "collector/output.json", report_rel])
    collector = json.loads((RUN_DIR / "collector" / "output.json").read_text(encoding="utf-8"))
    assert set(collector) == {"_note", "database_schema"}  # no query text, no metadata
    assert collected_identifiers(collector)  # the names the executive report keeps out


def test_committed_run_carries_no_private_detail() -> None:
    for path in sorted(p for p in RUN_DIR.rglob("*") if p.is_file()):
        found = build_docs_sample.private_markers(path.read_text(encoding="utf-8"))
        assert found == [], (path.name, found)


def test_committed_run_readme_names_the_release_the_sample_page_names() -> None:
    readme = (RUN_DIR / "README.md").read_text(encoding="utf-8")
    page = (build_docs_sample.REPO / "docs" / "sample.md").read_text(encoding="utf-8")
    contract = _committed_report()["contract_version"]
    assert f"| Synthesis contract | {contract} |" in readme
    release = next(
        line.split("|")[2].strip() for line in readme.splitlines() if line.startswith("| Release")
    )
    assert release.startswith("v") and release in page


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ('{"path": "/Users/someone/repo"}', "local path"),
        ('{"path": "/home/someone/repo"}', "local path"),
        ('{"path": "C:\\\\Users\\\\someone"}', "local path"),
        ("contact someone@example.com", "e-mail address"),
        ("account 123456789012 here", "AWS account id"),
        ("arn:aws:iam::role/x", "AWS ARN"),
        # Built at runtime so no scanner reads a key-shaped literal in this file.
        pytest.param("key " + "AKIA" + "Z" * 16, "AWS access key id", id="access-key-id"),
        ('{"path": "/var/folders/ab/T/x"}', "local path"),
        ('{"path": "/opt/app/artifacts"}', "local path"),
        ('{"path": "/root/repo"}', "local path"),
        ("see ~/Documents/repo", "local path"),
        ("https://wiki.example.org/page", "non-public URL"),
        ("s3://some-bucket/key", "non-public URL"),
        ("db.internal.example.com", "internal hostname"),
        ("wiki.corp.example", "internal hostname"),
        ("http://localhost:8000/x", "internal hostname"),
        ("host 10.0.3.4", "IP address"),
        ("ip-10-0-3-4.ec2.internal", "IP address"),
        pytest.param("token " + "sk-" + "ant-" + "x" * 30, "API token", id="model-api-token"),
        pytest.param("token " + "ghp" + "_" + "x" * 36, "API token", id="repo-token"),
        pytest.param("aws_" + "secret_access_key", "API token", id="secret-key-name"),
        pytest.param("-----BEGIN " + "RSA PRIVATE KEY-----", "private key", id="private-key"),
    ],
)
def test_private_markers_find_each_kind(text: str, kind: str) -> None:
    assert kind in build_docs_sample.private_markers(text)


@pytest.mark.parametrize(
    "text",
    [
        '{"frequency_per_hour": 12925.083333333334}',  # a float's 12-digit fraction
        f'"{hashlib.sha256(b"SELECT 1").hexdigest()}"',  # a query id: 64 hex characters
        "SELECT * FROM wp_users WHERE user_email = ?",
        "550e8400-e29b-41d4-a716-446655440000",  # a UUID's 12-digit tail is no account id
        "MySQL 8.0.45 on Aurora MySQL 3.08",  # versions are not IP addresses
        "https://aws.amazon.com/dynamodb/ and https://docs.aws.amazon.com/x",  # public
        "https://aws-samples.github.io/sample-aws-genai-db-modernizer/sample/",
        "the deliverables folder of the job runner",  # a word, with no user names given
        "",
    ],
)
def test_private_markers_pass_ordinary_report_text(text: str) -> None:
    assert build_docs_sample.private_markers(text) == []


def test_private_markers_find_a_given_user_name_as_a_whole_word() -> None:
    names = ("jdoe",)
    assert build_docs_sample.private_markers("written by JDoe today", names) == ["user name"]
    assert build_docs_sample.private_markers("artifacts of jdoe/x", names) == ["user name"]
    assert build_docs_sample.private_markers("the jdoesmith table", names) == []
    assert build_docs_sample.private_markers("written by jdoe", ()) == []


def test_the_release_step_lists_every_kind_the_refresh_refuses() -> None:
    release = (build_docs_sample.REPO / "docs" / "RELEASE_MANAGEMENT.md").read_text(
        encoding="utf-8"
    )
    step = release[release.index("Refresh the help site's sample report") :]
    step = step[: step.index("**Step 3")]
    missing = [k for k in build_docs_sample.PRIVATE_DETAIL_KINDS if k not in step]
    assert missing == []
    assert "a person reads the diff" in step


def test_the_committed_secrets_baseline_is_current_for_the_sample_run(tmp_path: Path) -> None:
    # The refresh regenerates the folder's baseline entries; regenerating them now
    # must change nothing, so the commit hook scans the committed files against
    # exactly what the last refresh recorded.
    committed = build_docs_sample.SCAN_BASELINE.read_text(encoding="utf-8")
    copy_ = tmp_path / ".secrets.baseline"
    copy_.write_text(committed, encoding="utf-8")
    counts = build_docs_sample.update_scan_baseline(RUN_DIR, copy_, build_docs_sample.REPO)
    assert copy_.read_text(encoding="utf-8") == committed
    assert sum(counts.values()) > 0  # the query ids are recorded, so the hook still runs
    assert build_docs_sample.private_markers(committed) == []  # no absolute path in it


def test_a_planted_key_in_the_sample_is_flagged_by_the_secret_scanner(tmp_path: Path) -> None:
    import secrets as _secrets

    from detect_secrets import SecretsCollection
    from detect_secrets.core import baseline as ds_baseline

    root = tmp_path / "repo"
    run_dir = root / "sample-run"
    shutil.copytree(RUN_DIR, run_dir)
    baseline_file = tmp_path / ".secrets.baseline"
    shutil.copyfile(build_docs_sample.SCAN_BASELINE, baseline_file)
    report_path = build_docs_sample.sample_run_report(run_dir)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    alphabet = string.ascii_uppercase + "234567"
    key = "AKIA" + "".join(_secrets.choice(alphabet) for _ in range(16))
    report["summary_llm"] += f" The key {key} was used."
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    data = json.loads(baseline_file.read_text(encoding="utf-8"))
    ds_baseline.load(data, str(baseline_file))
    scanned = SecretsCollection(root=str(root))
    rel = report_path.relative_to(root).as_posix()
    scanned.scan_file(rel)
    known = {
        e["hashed_secret"]
        for k, v in data["results"].items()
        if k.endswith("sample-run/" + report_path.relative_to(run_dir).as_posix())
        for e in v
    }
    (found,) = scanned.json().values()  # the one scanned file
    new = [e for e in found if e["hashed_secret"] not in known]
    assert [e["type"] for e in new] == ["AWS Access Key"]
    assert "AWS access key id" in build_docs_sample.private_markers(report_path.read_text())


def _fake_job(tmp_path: Path, report_extra: str = "") -> Path:
    job = tmp_path / "job"
    (job / "synthesis" / "v1").mkdir(parents=True)
    (job / "synthesis" / "v3").mkdir(parents=True)
    (job / "synthesis" / "v1" / "report.json").write_text('{"old": true}', encoding="utf-8")
    (job / "synthesis" / "v3" / "report.json").write_text(
        '{"job_id": "j1", "database_name": "wordpress"' + report_extra + "}", encoding="utf-8"
    )
    (job / "collector").mkdir()
    collector = {
        "metadata": {"source_database": {"hostname": "db.example"}},
        "queries": [{"sql": "SELECT 1"}],
        "database_schema": {
            "tables": [
                {
                    "table_id": "wordpress.wp_posts",
                    "table_name": "wp_posts",
                    "schema_name": "wordpress",
                    "row_count": 10,
                    "columns": [{"column_name": "ID", "data_type": "bigint"}],
                }
            ],
            "views": [],
        },
    }
    (job / "collector" / "output.json").write_text(json.dumps(collector), encoding="utf-8")
    return job


def test_refresh_copies_the_latest_report_and_only_the_collected_names(tmp_path: Path) -> None:
    job = _fake_job(tmp_path)
    run_dir = tmp_path / "sample-run"
    shutil.copytree(RUN_DIR, run_dir)
    readme = (run_dir / "README.md").read_text(encoding="utf-8")
    baseline_file = tmp_path / ".secrets.baseline"
    shutil.copyfile(build_docs_sample.SCAN_BASELINE, baseline_file)
    before = json.loads(baseline_file.read_text(encoding="utf-8"))["results"]

    result = build_docs_sample.refresh_sample_run(job, run_dir, baseline_file, tmp_path)

    assert (run_dir / "README.md").read_text(encoding="utf-8") == readme  # kept
    assert sorted(p.name for p in (run_dir / "synthesis").iterdir()) == ["v3"]  # old ones gone
    copied = (run_dir / "synthesis" / "v3" / "report.json").read_text(encoding="utf-8")
    assert copied == (job / "synthesis" / "v3" / "report.json").read_text(encoding="utf-8") + "\n"
    names = json.loads((run_dir / "collector" / "output.json").read_text(encoding="utf-8"))
    original = json.loads((job / "collector" / "output.json").read_text(encoding="utf-8"))
    assert collected_identifiers(names) == collected_identifiers(original)
    assert "metadata" not in names and "queries" not in names
    assert names["database_schema"]["tables"] == [
        {
            "table_id": "wordpress.wp_posts",
            "table_name": "wp_posts",
            "schema_name": "wordpress",
            "columns": [{"column_name": "ID"}],
        }
    ]
    # Its baseline entries are regenerated (none: the fake job has no high-entropy
    # text), repo-relative, and every other file's entries stay as they were.
    assert set(result["scan_baseline_entries"]) == {
        "sample-run/README.md",
        "sample-run/collector/output.json",
        "sample-run/synthesis/v3/report.json",
    }
    after = json.loads(baseline_file.read_text(encoding="utf-8"))["results"]
    assert after == before


def test_refresh_records_the_new_report_s_query_ids_in_the_baseline(tmp_path: Path) -> None:
    run_dir = tmp_path / "sample-run"
    shutil.copytree(RUN_DIR, run_dir)
    job = tmp_path / "job"
    shutil.copytree(RUN_DIR, job)
    shutil.copyfile(  # a full collector output, as a real job has
        RUN_DIR / "collector" / "output.json", job / "collector" / "output.json"
    )
    baseline_file = tmp_path / ".secrets.baseline"
    shutil.copyfile(build_docs_sample.SCAN_BASELINE, baseline_file)

    result = build_docs_sample.refresh_sample_run(job, run_dir, baseline_file, tmp_path)

    report_rel = (
        "sample-run/" + build_docs_sample.sample_run_report(run_dir).relative_to(run_dir).as_posix()
    )
    entries = json.loads(baseline_file.read_text(encoding="utf-8"))["results"][report_rel]
    assert len(entries) == result["scan_baseline_entries"][report_rel] > 0
    assert {e["filename"] for e in entries} == {report_rel}  # relative, no local path


def test_refresh_refuses_a_job_with_a_private_detail(tmp_path: Path) -> None:
    job = _fake_job(tmp_path, ', "path": "/Users/someone/artifacts"')
    run_dir = tmp_path / "sample-run"
    shutil.copytree(RUN_DIR, run_dir)
    before = sorted(p.relative_to(run_dir) for p in run_dir.rglob("*"))

    baseline_file = tmp_path / ".secrets.baseline"
    shutil.copyfile(build_docs_sample.SCAN_BASELINE, baseline_file)
    baseline = baseline_file.read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="local path"):
        build_docs_sample.refresh_sample_run(job, run_dir, baseline_file, tmp_path)
    assert baseline_file.read_text(encoding="utf-8") == baseline
    assert sorted(p.relative_to(run_dir) for p in run_dir.rglob("*")) == before  # untouched


def test_a_report_of_another_major_is_refused(tmp_path: Path) -> None:
    from src.shared.contract_versioning import ContractVersionError

    run_dir = tmp_path / "sample-run"
    shutil.copytree(RUN_DIR, run_dir)
    report_path = build_docs_sample.sample_run_report(run_dir)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["contract_version"] = "99.0"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(ContractVersionError):
        build_docs_sample.render_sample_run(run_dir, tmp_path / "artifacts")
    with pytest.raises(ContractVersionError):
        build_docs_sample.refresh_sample_run(
            run_dir, tmp_path / "other-run", tmp_path / ".secrets.baseline", tmp_path
        )


def test_gitleaks_scans_the_sample_run() -> None:
    # gitleaks allowlists docs prose and the sample collections by path; none of
    # those paths may cover the recorded run, or no CI scanner reads it.
    import tomllib

    config = tomllib.loads((build_docs_sample.REPO / ".gitleaks.toml").read_text(encoding="utf-8"))
    patterns = [re.compile(p) for p in config["allowlist"]["paths"]]
    files = [
        p.relative_to(build_docs_sample.REPO).as_posix() for p in RUN_DIR.rglob("*") if p.is_file()
    ]
    assert files
    covered = [f for f in files if any(p.search(f) for p in patterns)]
    assert covered == []
    # ...while the paths the allowlist is there for stay covered.
    for path in (
        "docs/faq.md",
        "docs/architecture/decisions/ADR-032-one-facts-layer-renderers-only-format.md",
        "docs/examples/wordpress/wordpress-collection.json",
        "docs/examples/wordpress/load-test-results/schema-dynamodb/v2/schema_output.json",
    ):
        assert any(p.search(path) for p in patterns), path
