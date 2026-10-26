"""Every deliverable names the code commit it was built from (#1166).

One value per render (``report_facts.build_commit``), carried in each file's identity
block and printed by every renderer: the engineering reports' last line, the executive
report's page footer and the folder index. It stays out of ``report.json`` and its
facts, so the figures stay deterministic.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts import run_report
from src.report import deliverables as dl
from src.report import executive_report
from src.report.facts import report_facts
from src.shared import report_facts as rf
from src.storage.local_store import LocalArtifactStore
from tests.executive_pdf import body_text, page_texts

REPO = Path(__file__).resolve().parents[3]
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "e2e09_report.json"
DB, JOB = "discourse", "job-x"
KEY = f"{DB}/{JOB}/synthesis/v1/report.json"
COMMIT = "abc1234def56"  # pragma: allowlist secret (a made-up commit)


@pytest.fixture
def store(tmp_path: Path) -> LocalArtifactStore:
    s = LocalArtifactStore(base_dir=str(tmp_path))
    s.write_json(KEY, json.loads(FIXTURE.read_text()))
    return s


class TestBuildCommit:
    def test_the_override_wins(self, monkeypatch):
        monkeypatch.setenv(rf.BUILD_COMMIT_ENV, COMMIT)
        assert rf.build_commit() == COMMIT

    @pytest.mark.parametrize("value", ["", "   ", "abc\n# Heading", 'a"b', "x" * 41, "a b"])
    def test_an_unusable_override_is_unknown_or_ignored(self, monkeypatch, value):
        monkeypatch.setenv(rf.BUILD_COMMIT_ENV, value)
        monkeypatch.setattr(rf, "_git_head", lambda: None)
        assert rf.build_commit() == rf.UNKNOWN_COMMIT

    def test_the_checkout_head_without_an_override(self, monkeypatch):
        monkeypatch.delenv(rf.BUILD_COMMIT_ENV, raising=False)
        head = subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", "--short=12", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert rf.build_commit() == head

    def test_no_git_is_unknown(self, monkeypatch):
        monkeypatch.delenv(rf.BUILD_COMMIT_ENV, raising=False)

        def fail(*args, **kwargs):
            raise FileNotFoundError("git")

        monkeypatch.setattr(rf.subprocess, "run", fail)
        assert rf.build_commit() == rf.UNKNOWN_COMMIT

    def test_one_line_wording(self):
        assert rf.build_commit_line(COMMIT) == f"Built from code commit {COMMIT}."


class TestEveryDeliverablePrintsIt:
    @pytest.fixture
    def rendered(self, store, monkeypatch):
        monkeypatch.setenv(rf.BUILD_COMMIT_ENV, COMMIT)
        return dl.render_deliverables(store, JOB, DB, KEY, assignment_version=1)

    def test_the_render_computes_it_once(self, rendered):
        assert rendered.commit == COMMIT

    @pytest.mark.parametrize("name", ["engineering-report", "engineering-report-detailed"])
    def test_the_markdown_reports_carry_it_in_front_matter_and_footer(self, rendered, name):
        md = next(d for d in rendered.items if d.name == name).content.decode("utf-8")
        front = md.split("---", 2)[1]
        assert f'commit: "{COMMIT}"' in front
        assert md.rstrip().splitlines()[-1] == rf.build_commit_line(COMMIT)

    def test_the_assessment_data_names_it_in_its_identity_only(self, rendered, store):
        item = next(d for d in rendered.items if d.name == "assessment-data")
        data = json.loads(item.content)
        assert data.pop("_artifact")["commit"] == COMMIT
        # The figures stay deterministic: the report and its facts never hold it. The
        # data is the report the deliverables render: its trade-offs state each long
        # sentence once (#1574; a new synthesis writes them so, this older one is read so).
        from src.shared.design_text import said_once_report
        from src.shared.migration_risk_rules import backfill_report_risks

        assert data == said_once_report(backfill_report_risks(store.read_json(KEY)))
        assert COMMIT not in json.dumps(report_facts(store.read_json(KEY)))

    def test_every_pdf_page_footer_carries_it(self, rendered):
        pdf = next(d for d in rendered.items if d.name == "executive-report").content
        line = rf.build_commit_line(COMMIT)
        assert all(line in page for page in page_texts(pdf))
        # Page furniture, never body text.
        assert COMMIT not in body_text(pdf, DB)

    def test_the_index_carries_it(self, rendered):
        index = dl.render_folder_index(
            DB, JOB, 1, [e for e, _ in rendered.folder_files()], commit=rendered.commit
        )
        assert f"Code commit: {COMMIT}" in index.splitlines()

    def test_run_report_writes_the_same_commit_everywhere(self, tmp_path, monkeypatch):
        monkeypatch.setenv(rf.BUILD_COMMIT_ENV, COMMIT)
        s = LocalArtifactStore(base_dir=str(tmp_path))
        s.write_json(KEY, json.loads(FIXTURE.read_text()))
        run_report.run(JOB, DB, str(tmp_path), assignment_version=None)
        folder = tmp_path / DB / JOB / "deliverables"
        assert f"Code commit: {COMMIT}" in (folder / "index.md").read_text()
        for name in ("engineering-report.md", "engineering-report-detailed.md"):
            assert rf.build_commit_line(COMMIT) in (folder / name).read_text()


@pytest.mark.passes_on_main
def test_a_direct_render_without_a_commit_prints_no_line():
    """Renderers only print the value they are given; they never look it up."""
    pdf = executive_report.render_executive_report_pdf(json.loads(FIXTURE.read_text()))
    assert "Built from code commit" not in " ".join(page_texts(pdf))
