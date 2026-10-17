"""The results page's download panel: list and download a job's deliverables (#957).

``GET /api/v1/assessments/{job_id}/deliverables`` lists the files of the job's
``deliverables/`` folder (#955), in the order and with the titles of
``src.report.deliverables.FOLDER_ENTRIES`` plus the folder's ``index.md``.
``GET /api/v1/assessments/{job_id}/deliverables/{filename}`` downloads one of
them. Only a file that list names can be served, and only from inside that
job's own ``deliverables/`` folder: a traversal, an encoded separator, a file
outside the list, a symlink out of the folder and another job's folder are all
refused.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.routes import results
from src.api.services.local_execution import LocalExecutionService
from src.api.services.local_s3 import LocalS3Service
from src.report.deliverables import (
    DELIVERABLES_DIR,
    FOLDER_ENTRIES,
    INDEX_FILENAME,
    RETIRED_FOLDER_FILENAMES,
)
from src.storage.local_store import LocalArtifactStore

client = TestClient(app)

DB = "wordpress"
JOB = "job-1"
CONTENT = {
    "executive-report.pdf": b"%PDF-1.7 fake",
    "engineering-report.md": b"# Engineering report\n",
    "assessment-data.json": b'{"facts": {}}',
    "index.md": b"# Database Modernizer Assessment Tool: deliverables for wordpress\n",
}


@pytest.fixture
def root(tmp_path: Path):
    """A real artifact root with one job and its deliverables folder."""
    store = LocalArtifactStore(str(tmp_path))
    results.sfn_service = LocalExecutionService(store)
    results.s3_service = LocalS3Service(store)
    folder = tmp_path / DB / JOB / DELIVERABLES_DIR
    folder.mkdir(parents=True)
    for name, body in CONTENT.items():
        (folder / name).write_bytes(body)
    yield tmp_path
    results.sfn_service = None
    results.s3_service = None


def _listing() -> dict:
    response = client.get(f"/api/v1/assessments/{JOB}/deliverables")
    assert response.status_code == 200, response.text
    data: dict = response.json()
    return data


class TestListDeliverables:
    def test_lists_the_files_on_disk_in_the_folder_order(self, root: Path) -> None:
        data = _listing()
        assert data["job_id"] == JOB
        expected = [e.filename for e in FOLDER_ENTRIES if e.filename in CONTENT] + [INDEX_FILENAME]
        assert [f["filename"] for f in data["files"]] == expected

    def test_each_entry_carries_its_title_audience_size_and_download_path(self, root: Path) -> None:
        by_name = {f["filename"]: f for f in _listing()["files"]}
        pdf = by_name["executive-report.pdf"]
        entry = next(e for e in FOLDER_ENTRIES if e.filename == "executive-report.pdf")
        assert pdf["title"] == entry.title
        assert pdf["audience"] == entry.audience
        assert pdf["size_bytes"] == len(CONTENT["executive-report.pdf"])
        assert pdf["path"] == f"assessments/{JOB}/deliverables/executive-report.pdf"
        assert by_name[INDEX_FILENAME]["title"]

    def test_leaves_out_a_listed_file_that_was_not_rendered(self, root: Path) -> None:
        (root / DB / JOB / DELIVERABLES_DIR / "executive-report.pdf").unlink()
        names = [f["filename"] for f in _listing()["files"]]
        assert "executive-report.pdf" not in names
        assert "engineering-report.md" in names

    def test_never_lists_a_file_the_folder_definition_does_not_name(self, root: Path) -> None:
        folder = root / DB / JOB / DELIVERABLES_DIR
        (folder / "llm_input.json").write_text("{}")
        (folder / "notes.txt").write_text("x")
        (folder / "sub").mkdir()
        names = [f["filename"] for f in _listing()["files"]]
        assert "llm_input.json" not in names and "notes.txt" not in names and "sub" not in names

    def test_a_job_without_the_folder_lists_nothing(self, root: Path) -> None:
        (root / DB / "job-2").mkdir()
        response = client.get("/api/v1/assessments/job-2/deliverables")
        assert response.status_code == 200
        assert response.json()["files"] == []

    def test_unknown_job_is_404(self, root: Path) -> None:
        assert client.get("/api/v1/assessments/nope/deliverables").status_code == 404

    def test_a_symlinked_deliverables_folder_is_not_listed(
        self, root: Path, tmp_path_factory
    ) -> None:
        outside = tmp_path_factory.mktemp("outside")
        (outside / "executive-report.pdf").write_bytes(b"secret")
        (root / DB / "job-3").mkdir()
        os.symlink(outside, root / DB / "job-3" / DELIVERABLES_DIR)
        response = client.get("/api/v1/assessments/job-3/deliverables")
        assert response.status_code == 200
        assert response.json()["files"] == []


class TestDownloadDeliverable:
    @pytest.mark.parametrize("name", sorted(CONTENT))
    def test_downloads_each_listed_file_as_an_attachment(self, root: Path, name: str) -> None:
        response = client.get(f"/api/v1/assessments/{JOB}/deliverables/{name}")
        assert response.status_code == 200
        assert response.content == CONTENT[name]
        assert response.headers["content-disposition"] == f'attachment; filename="{name}"'
        assert response.headers["x-content-type-options"] == "nosniff"

    @pytest.mark.parametrize(
        ("name", "media"),
        [
            ("executive-report.pdf", "application/pdf"),
            ("engineering-report.md", "text/markdown"),
            ("assessment-data.json", "application/json"),
            ("index.md", "text/markdown"),
        ],
    )
    def test_serves_each_type_with_its_media_type(self, root: Path, name: str, media: str) -> None:
        response = client.get(f"/api/v1/assessments/{JOB}/deliverables/{name}")
        assert response.headers["content-type"].split(";")[0] == media

    @pytest.mark.parametrize(
        "path",
        [
            "..%2F..%2F_meta.json",
            "..%2Fcollector%2Foutput.json",
            "%2E%2E",
            "..%5C..%5Cexecutive-report.pdf",
            "executive-report.pdf%00.md",
        ],
    )
    def test_refuses_a_traversal_or_encoded_separator(self, root: Path, path: str) -> None:
        (root / DB / JOB / "_meta.json").write_text('{"secret": true}')
        response = client.get(f"/api/v1/assessments/{JOB}/deliverables/{path}")
        assert response.status_code in (404, 422), (path, response.status_code)
        assert b"secret" not in response.content

    def test_a_plain_traversal_never_reaches_another_route_s_file(self, root: Path) -> None:
        (root / DB / JOB / "_meta.json").write_text('{"secret": true}')
        response = client.get(f"/api/v1/assessments/{JOB}/deliverables/../../_meta.json")
        assert b"secret" not in response.content

    def test_refuses_a_file_in_the_folder_that_the_definition_does_not_name(
        self, root: Path
    ) -> None:
        folder = root / DB / JOB / DELIVERABLES_DIR
        (folder / "llm_input.json").write_text('{"secret": true}')
        response = client.get(f"/api/v1/assessments/{JOB}/deliverables/llm_input.json")
        assert response.status_code == 404
        assert b"secret" not in response.content

    def test_a_listed_name_that_was_not_rendered_is_404(self, root: Path) -> None:
        (root / DB / JOB / DELIVERABLES_DIR / "executive-report.pdf").unlink()
        assert (
            client.get(f"/api/v1/assessments/{JOB}/deliverables/executive-report.pdf").status_code
            == 404
        )

    def test_refuses_a_listed_name_that_is_a_symlink_out_of_the_folder(
        self, root: Path, tmp_path_factory
    ) -> None:
        outside = tmp_path_factory.mktemp("outside") / "secret.pdf"
        outside.write_bytes(b"secret")
        target = root / DB / JOB / DELIVERABLES_DIR / "executive-report.pdf"
        target.unlink()
        os.symlink(outside, target)
        response = client.get(f"/api/v1/assessments/{JOB}/deliverables/executive-report.pdf")
        assert response.status_code == 404
        assert b"secret" not in response.content

    def test_refuses_a_symlinked_deliverables_folder(self, root: Path, tmp_path_factory) -> None:
        outside = tmp_path_factory.mktemp("outside")
        (outside / "executive-report.pdf").write_bytes(b"secret")
        (root / DB / "job-3").mkdir()
        os.symlink(outside, root / DB / "job-3" / DELIVERABLES_DIR)
        response = client.get("/api/v1/assessments/job-3/deliverables/executive-report.pdf")
        assert response.status_code == 404
        assert b"secret" not in response.content

    def test_serves_only_the_requested_job_s_own_folder(self, root: Path) -> None:
        other = root / DB / "job-2" / DELIVERABLES_DIR
        other.mkdir(parents=True)
        (other / "executive-report.pdf").write_bytes(b"job-2 pdf")
        response = client.get(f"/api/v1/assessments/{JOB}/deliverables/executive-report.pdf")
        assert response.content == CONTENT["executive-report.pdf"]
        response = client.get("/api/v1/assessments/job-2/deliverables/executive-report.pdf")
        assert response.content == b"job-2 pdf"

    def test_an_invalid_job_id_is_422(self, root: Path) -> None:
        response = client.get(
            "/api/v1/assessments/..%2Fwordpress/deliverables/executive-report.pdf"
        )
        assert response.status_code in (404, 422)

    @pytest.mark.parametrize("name", RETIRED_FOLDER_FILENAMES)
    def test_a_retired_html_report_an_older_round_left_is_not_served(
        self, root: Path, name: str
    ) -> None:
        """#987: a job rendered before the HTML reports were retired still has
        them in its folder. They are not listed and never downloaded."""
        (root / DB / JOB / DELIVERABLES_DIR / name).write_bytes(b"<html>old</html>")
        response = client.get(f"/api/v1/assessments/{JOB}/deliverables/{name}")
        assert response.status_code == 404
        assert b"old" not in response.content
        assert name not in {f["filename"] for f in _listing()["files"]}

    def test_no_file_carries_a_sandbox_policy_any_more(self, root: Path) -> None:
        response = client.get(f"/api/v1/assessments/{JOB}/deliverables/executive-report.pdf")
        assert "content-security-policy" not in response.headers


def _outside_job(root: Path, tmp_path_factory, db_name_for: str) -> str:
    """A job whose _meta.json names a database outside the artifact root: an
    ``outside`` directory holding ``<job>/deliverables/engineering-report.md``.
    Returns the job id; ``db_name_for`` is "absolute" or "traversal"."""
    outside = tmp_path_factory.mktemp("outside")
    job = "job-meta"
    folder = outside / job / DELIVERABLES_DIR
    folder.mkdir(parents=True)
    (folder / "engineering-report.md").write_bytes(b"secret outside the root")
    if db_name_for == "absolute":
        db = os.path.realpath(outside)
    else:
        db = os.path.relpath(os.path.realpath(outside), os.path.realpath(root / DB))
        db = os.path.join("..", db) if not db.startswith("..") else db
    job_dir = root / DB / job
    job_dir.mkdir()
    (job_dir / "_meta.json").write_text(json.dumps({"input": {"database_name": db}}))
    return job


@pytest.mark.parametrize("kind", ["absolute", "traversal"])
class TestMetaDatabaseName:
    """The database name comes from the job's _meta.json, not the request; it
    is validated like request input, and the file must sit under the artifact
    root whatever it says (#961 review)."""

    def test_download_is_refused(self, root: Path, tmp_path_factory, kind: str) -> None:
        job = _outside_job(root, tmp_path_factory, kind)
        response = client.get(f"/api/v1/assessments/{job}/deliverables/engineering-report.md")
        assert response.status_code == 404
        assert b"secret" not in response.content

    def test_listing_is_refused(self, root: Path, tmp_path_factory, kind: str) -> None:
        job = _outside_job(root, tmp_path_factory, kind)
        response = client.get(f"/api/v1/assessments/{job}/deliverables")
        assert response.status_code == 404
        assert b"engineering-report" not in response.content

    def test_the_service_alone_refuses_it_too(
        self, root: Path, tmp_path_factory, kind: str
    ) -> None:
        """Defense in depth: without the route's validation, the service's
        artifact-root check still refuses the file."""
        job = _outside_job(root, tmp_path_factory, kind)
        meta = json.loads((root / DB / job / "_meta.json").read_text())
        db = meta["input"]["database_name"]
        svc = results.sfn_service
        try:
            path = svc.deliverable_file(db, job, "engineering-report.md")
        except ValueError:
            path = None
        assert path is None


def test_listing_is_empty_when_a_file_check_raises(root: Path, monkeypatch) -> None:
    """A job folder swapped for a symlink between the folder check and a file
    check makes the resolver raise; the listing returns no files, not a 500
    (#961 review)."""

    def boom(*args, **kwargs):
        raise ValueError("path escapes the artifact root")

    monkeypatch.setattr(results.sfn_service, "deliverable_file", boom)
    response = client.get(f"/api/v1/assessments/{JOB}/deliverables")
    assert response.status_code == 200
    assert response.json()["files"] == []
