"""LocalExecutionService.job_status / delete_job_artifacts -- the backend
for DELETE /assessments/{job_id}/artifacts (#529), which deletes a finished
local job's artifact folder and orchestrator marker from disk.

Round-1 security review (see the #535 PR thread) found that the original
``os.path.realpath`` + ``startswith`` containment check -- "resolves to
somewhere inside the root" -- lets a symlink alias one job's folder (or
database folder, or the root itself) onto another, since the resolved path
can still land inside the root while pointing at the wrong thing. The fix
in ``_resolve_job_dir``/``_resolve_meta_path`` requires two things instead:
neither the database directory nor the job directory (nor ``.meta`` nor
the marker file) may be a symlink at all, and the resolved path must
*exactly* equal the literal, unresolved join -- not merely be contained by
it. This file's "aliasing" tests exercise exactly that gap: each one would
pass the weaker containment check (the resolved path is still "inside" the
root) but must still be refused.

Deletion itself also gained, in the same review: a refusal if any file
under the job folder (or its ``.meta`` marker) was modified in the last
``_STALE_WINDOW_SECONDS`` (a concurrent write might still be in progress);
a ``_status.json`` CANCELLED marker written before anything is removed, so
a racing resume/confirm-all sees a terminal job; a tombstone-based delete
(``os.rename`` into ``.trash/`` then ``shutil.rmtree`` the tombstone,
rather than ``rmtree`` in place) so the job's original path disappears
atomically; and a rule that leaves ``.meta/{job_id}.json`` alone if another
database's folder still uses the same ``job_id``.

Outside directories use ``tmp_path_factory`` (a fresh top-level temp dir
per call), not ``tmp_path.parent`` -- the latter is still inside pytest's
own shared temp root and isn't a meaningfully separate location.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from src.api.services.local_execution import (
    ArtifactDeletionError,
    JobRecentlyModifiedError,
    LocalExecutionService,
)
from src.storage.local_store import LocalArtifactStore

DB, JOB = "wordpress", "job-001"


def _service(tmp_path: Path) -> tuple[LocalExecutionService, LocalArtifactStore]:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    return LocalExecutionService(store), store


def _write_collector(store: LocalArtifactStore, db: str = DB, job: str = JOB) -> None:
    store.write_json(
        f"{db}/{job}/collector/output.json",
        {"metadata": {"source_database": {"engine": "mysql", "database_name": db}}},
    )


def _age_everything_under(root: Path, seconds: int = 3600) -> None:
    """Push every file's mtime back far enough that the staleness guard
    (default window: 60s) never fires for fixtures set up well before the
    call under test -- the guard is tested on its own terms below."""
    old = time.time() - seconds
    for walk_root, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(walk_root, name)
            os.utime(path, (old, old), follow_symlinks=False)


@pytest.fixture(autouse=True)
def _disable_staleness_guard(monkeypatch):
    """Every test in this file sets up its fixtures immediately before
    calling the method under test, which would otherwise always trip the
    staleness guard. Disabling it here by default keeps every other test
    focused on what it's actually testing; TestStaleness below restores a
    real (short) window to test the guard itself."""
    import src.api.services.local_execution as local_execution_module

    monkeypatch.setattr(local_execution_module, "_STALE_WINDOW_SECONDS", -1)


# ---------------------------------------------------------------------------
# job_status
# ---------------------------------------------------------------------------


class TestJobStatus:
    def test_returns_none_for_an_unknown_job(self, tmp_path: Path) -> None:
        service, _ = _service(tmp_path)
        assert service.job_status(DB, JOB) is None

    def test_returns_running_for_a_bare_job_folder(self, tmp_path: Path) -> None:
        service, store = _service(tmp_path)
        _write_collector(store)
        assert service.job_status(DB, JOB) == "RUNNING"

    def test_returns_cancelled_after_stop_execution(self, tmp_path: Path) -> None:
        service, store = _service(tmp_path)
        _write_collector(store)
        service.stop_execution(JOB)
        assert service.job_status(DB, JOB) == "CANCELLED"

    def test_does_not_find_the_job_under_the_wrong_database_name(self, tmp_path: Path) -> None:
        service, store = _service(tmp_path)
        _write_collector(store)
        assert service.job_status("other_db", JOB) is None

    def test_rejects_a_traversal_database_name(self, tmp_path: Path) -> None:
        service, store = _service(tmp_path)
        _write_collector(store)
        with pytest.raises(ValueError, match="escapes the artifact root"):
            service.job_status("../escape", JOB)

    def test_rejects_a_symlinked_job_folder_escaping_the_root(
        self, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        service, _store = _service(tmp_path)
        outside = tmp_path_factory.mktemp("outside-status")
        (tmp_path / DB).mkdir(parents=True, exist_ok=True)
        os.symlink(outside, tmp_path / DB / JOB)

        with pytest.raises(ValueError, match="escapes the artifact root"):
            service.job_status(DB, JOB)


# ---------------------------------------------------------------------------
# delete_job_artifacts -- normal use
# ---------------------------------------------------------------------------


class TestDeleteJobArtifacts:
    def test_returns_none_when_nothing_exists(self, tmp_path: Path) -> None:
        service, _ = _service(tmp_path)
        assert service.delete_job_artifacts(DB, JOB) is None

    def test_removes_the_job_folder_and_lists_every_file(self, tmp_path: Path) -> None:
        service, store = _service(tmp_path)
        _write_collector(store)
        store.write_json(f"{DB}/{JOB}/referee-triage/triage.json", {"selected_agents": []})

        removed = service.delete_job_artifacts(DB, JOB)

        # delete_job_artifacts writes its own _status.json CANCELLED
        # marker before removing anything (round-1 I2), but round-2 S-b:
        # that marker isn't one of the job's own artifacts, so it's not
        # reported in `removed` even though it genuinely gets deleted too.
        assert removed == sorted(
            [
                f"{DB}/{JOB}/collector/output.json",
                f"{DB}/{JOB}/referee-triage/triage.json",
            ]
        )
        assert not (tmp_path / DB / JOB).exists()

    def test_removes_the_orchestrator_marker_too(self, tmp_path: Path) -> None:
        service, store = _service(tmp_path)
        _write_collector(store)
        store.write_json(f".meta/{JOB}.json", {"job_id": JOB, "phases": {}})

        removed = service.delete_job_artifacts(DB, JOB)

        assert f".meta/{JOB}.json" in removed
        assert f"{DB}/{JOB}/collector/output.json" in removed
        assert not (tmp_path / ".meta" / f"{JOB}.json").exists()
        assert not (tmp_path / DB / JOB).exists()

    def test_removes_only_the_marker_when_the_job_folder_is_absent(self, tmp_path: Path) -> None:
        service, store = _service(tmp_path)
        store.write_json(f".meta/{JOB}.json", {"job_id": JOB, "phases": {}})

        removed = service.delete_job_artifacts(DB, JOB)

        assert removed == [f".meta/{JOB}.json"]
        assert not (tmp_path / ".meta" / f"{JOB}.json").exists()

    def test_leaves_other_jobs_and_other_databases_untouched(self, tmp_path: Path) -> None:
        service, store = _service(tmp_path)
        _write_collector(store)
        _write_collector(store, db=DB, job="job-002")
        _write_collector(store, db="other_db", job=JOB)

        service.delete_job_artifacts(DB, JOB)

        assert not (tmp_path / DB / JOB).exists()
        assert (tmp_path / DB / "job-002").exists()
        assert (tmp_path / "other_db" / JOB).exists()

    def test_the_job_folder_disappears_from_its_original_path_via_rename(
        self, tmp_path: Path
    ) -> None:
        """The tombstone move (os.rename), not the slower rmtree, is what
        makes the original path vanish -- so it must be gone even if we
        could somehow observe mid-deletion state."""
        service, store = _service(tmp_path)
        _write_collector(store)
        service.delete_job_artifacts(DB, JOB)
        assert not (tmp_path / DB / JOB).exists()
        # Nothing is left behind under .trash/ on the happy path either.
        trash = tmp_path / ".trash"
        assert not trash.exists() or list(trash.iterdir()) == []


class TestWritesCancelledMarkerBeforeDeleting:
    """A CANCELLED marker is written before anything is removed, so a
    concurrent resume/confirm-all call sees a terminal job even if the
    delete fails partway through (round-1 review, I2). It's written
    directly at the validated ``{database_name}/{job_id}/_status.json``
    path -- *not* via ``stop_execution`` (round-2 review, B), which
    resolves ``job_id`` by scanning for the first database directory that
    happens to contain it, and would silently mark the wrong job's folder
    if two databases share a ``job_id``."""

    def test_marker_is_written_for_an_existing_job_folder(self, tmp_path: Path) -> None:
        service, store = _service(tmp_path)
        _write_collector(store)

        with patch.object(service._store, "write_json", wraps=store.write_json) as spy:
            service.delete_job_artifacts(DB, JOB)

        marker_calls = [
            call for call in spy.call_args_list if call.args[0] == f"{DB}/{JOB}/_status.json"
        ]
        assert len(marker_calls) == 1
        marker = marker_calls[0].args[1]
        assert marker["status"] == "CANCELLED"
        assert marker["last_phase"] is None
        assert marker["stopped_at"]

    def test_marker_is_not_written_when_only_the_orchestrator_marker_exists(
        self, tmp_path: Path
    ) -> None:
        """No job folder to protect -- there's nowhere to write a
        _status.json into, so nothing is written."""
        service, store = _service(tmp_path)
        store.write_json(f".meta/{JOB}.json", {"job_id": JOB, "phases": {}})

        with patch.object(service._store, "write_json", wraps=store.write_json) as spy:
            service.delete_job_artifacts(DB, JOB)
            for call in spy.call_args_list:
                assert "_status.json" not in call[0][0]

    def test_marks_only_the_targeted_database_when_job_id_collides(self, tmp_path: Path) -> None:
        """db1/jobB and db2/jobB share a job_id -- deleting db2/jobB must
        write (and only ever touch) db2/jobB/_status.json, never
        db1/jobB's, which stop_execution's job-id-only scan could have
        picked instead (round-2 review, B)."""
        service, store = _service(tmp_path)
        shared_job = "jobB"
        _write_collector(store, db="db1", job=shared_job)
        _write_collector(store, db="db2", job=shared_job)

        service.delete_job_artifacts("db2", shared_job)

        assert not (tmp_path / "db2" / shared_job).exists()
        # db1's job folder, and its status, are completely unaffected.
        assert (tmp_path / "db1" / shared_job).exists()
        assert not (tmp_path / "db1" / shared_job / "_status.json").exists()
        assert service.job_status("db1", shared_job) == "RUNNING"


class TestMetaMarkerSharedAcrossDatabases:
    """S1: .meta/{job_id}.json is keyed on job_id alone. If some other
    database's folder still uses the same job_id (a hand-picked ID
    collision -- job_id is meant to be globally unique, but nothing
    enforces that), deleting the marker would corrupt that other job's
    status, so it's left alone."""

    def test_meta_marker_survives_if_another_database_still_uses_the_job_id(
        self, tmp_path: Path
    ) -> None:
        service, store = _service(tmp_path)
        _write_collector(store, db=DB, job=JOB)
        _write_collector(store, db="other_db", job=JOB)
        store.write_json(f".meta/{JOB}.json", {"job_id": JOB, "phases": {}})

        removed = service.delete_job_artifacts(DB, JOB)

        assert f".meta/{JOB}.json" not in removed
        assert (tmp_path / ".meta" / f"{JOB}.json").exists()
        assert not (tmp_path / DB / JOB).exists()
        assert (tmp_path / "other_db" / JOB).exists()

    def test_meta_marker_is_removed_once_no_other_database_uses_the_job_id(
        self, tmp_path: Path
    ) -> None:
        service, store = _service(tmp_path)
        _write_collector(store, db=DB, job=JOB)
        store.write_json(f".meta/{JOB}.json", {"job_id": JOB, "phases": {}})

        removed = service.delete_job_artifacts(DB, JOB)

        assert f".meta/{JOB}.json" in removed
        assert not (tmp_path / ".meta" / f"{JOB}.json").exists()


# ---------------------------------------------------------------------------
# delete_job_artifacts -- staleness guard (I2)
# ---------------------------------------------------------------------------


class TestStaleness:
    def test_refuses_when_a_job_file_was_modified_within_the_window(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        import src.api.services.local_execution as local_execution_module

        monkeypatch.setattr(local_execution_module, "_STALE_WINDOW_SECONDS", 3600)
        service, store = _service(tmp_path)
        _write_collector(store)  # just written -- well within the window

        with pytest.raises(JobRecentlyModifiedError, match="job-001"):
            service.delete_job_artifacts(DB, JOB)
        assert (tmp_path / DB / JOB).exists()

    def test_refuses_when_the_meta_marker_was_modified_within_the_window(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        import src.api.services.local_execution as local_execution_module

        monkeypatch.setattr(local_execution_module, "_STALE_WINDOW_SECONDS", 3600)
        service, store = _service(tmp_path)
        _write_collector(store)
        _age_everything_under(tmp_path / DB)
        store.write_json(f".meta/{JOB}.json", {"job_id": JOB, "phases": {}})  # fresh

        with pytest.raises(JobRecentlyModifiedError, match="job-001"):
            service.delete_job_artifacts(DB, JOB)
        assert (tmp_path / ".meta" / f"{JOB}.json").exists()

    def test_proceeds_once_every_file_is_outside_the_window(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        import src.api.services.local_execution as local_execution_module

        monkeypatch.setattr(local_execution_module, "_STALE_WINDOW_SECONDS", 1)
        service, store = _service(tmp_path)
        _write_collector(store)
        _age_everything_under(tmp_path, seconds=10)

        removed = service.delete_job_artifacts(DB, JOB)
        assert removed == [f"{DB}/{JOB}/collector/output.json"]


# ---------------------------------------------------------------------------
# delete_job_artifacts / job_status -- aliasing (C1/C2) and traversal
# ---------------------------------------------------------------------------


class TestAliasingAndTraversal:
    def test_rejects_a_traversal_database_name_and_deletes_nothing(self, tmp_path: Path) -> None:
        service, store = _service(tmp_path)
        _write_collector(store)

        with pytest.raises(ValueError, match="escapes the artifact root"):
            service.delete_job_artifacts("../escape", JOB)

        assert (tmp_path / DB / JOB).exists()

    def test_rejects_a_traversal_job_id_and_deletes_nothing(self, tmp_path: Path) -> None:
        service, store = _service(tmp_path)
        _write_collector(store)

        with pytest.raises(ValueError, match="escapes the artifact root"):
            service.delete_job_artifacts(DB, "../../escape")

        assert (tmp_path / DB / JOB).exists()

    def test_rejects_a_symlinked_job_folder_escaping_the_root(
        self, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        service, _store = _service(tmp_path)
        outside = tmp_path_factory.mktemp("outside-delete")
        (outside / "secret.json").write_text("{}")
        (tmp_path / DB).mkdir(parents=True, exist_ok=True)
        os.symlink(outside, tmp_path / DB / JOB)

        with pytest.raises(ValueError, match="escapes the artifact root"):
            service.delete_job_artifacts(DB, JOB)

        assert (outside / "secret.json").exists()
        assert (tmp_path / DB / JOB).is_symlink()

    def test_rejects_a_symlinked_meta_marker_escaping_the_root(
        self, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        service, store = _service(tmp_path)
        _write_collector(store)
        outside = tmp_path_factory.mktemp("outside-meta")
        (outside / "secret.json").write_text("{}")
        (tmp_path / ".meta").mkdir(parents=True, exist_ok=True)
        os.symlink(outside / "secret.json", tmp_path / ".meta" / f"{JOB}.json")

        with pytest.raises(ValueError, match="escapes the artifact root"):
            service.delete_job_artifacts(DB, JOB)

        assert (outside / "secret.json").exists()
        assert (tmp_path / DB / JOB).exists()

    # -- aliasing: still "inside the root", which the old startswith-only
    # check would have accepted; the exact-match + islink checks must not.

    def test_rejects_a_job_folder_symlinked_to_another_jobs_folder(self, tmp_path: Path) -> None:
        service, store = _service(tmp_path)
        _write_collector(store, db=DB, job="job-002")
        real_other_job = tmp_path / DB / "job-002"
        (tmp_path / DB).mkdir(parents=True, exist_ok=True)
        os.symlink(real_other_job, tmp_path / DB / JOB)

        with pytest.raises(ValueError, match="escapes the artifact root"):
            service.delete_job_artifacts(DB, JOB)

        assert real_other_job.exists()
        assert (real_other_job / "collector" / "output.json").exists()

    def test_rejects_a_job_folder_symlinked_to_a_database_folder(self, tmp_path: Path) -> None:
        service, store = _service(tmp_path)
        _write_collector(store, db="other_db", job="job-002")
        real_other_db = tmp_path / "other_db"
        (tmp_path / DB).mkdir(parents=True, exist_ok=True)
        os.symlink(real_other_db, tmp_path / DB / JOB)

        with pytest.raises(ValueError, match="escapes the artifact root"):
            service.delete_job_artifacts(DB, JOB)

        assert (real_other_db / "job-002").exists()

    def test_rejects_a_job_folder_symlinked_to_the_artifact_root_itself(
        self, tmp_path: Path
    ) -> None:
        service, store = _service(tmp_path)
        _write_collector(store, db="other_db", job="job-002")
        (tmp_path / DB).mkdir(parents=True, exist_ok=True)
        os.symlink(tmp_path, tmp_path / DB / JOB)

        with pytest.raises(ValueError, match="escapes the artifact root"):
            service.delete_job_artifacts(DB, JOB)

        assert (tmp_path / "other_db" / "job-002").exists()

    def test_rejects_a_symlinked_database_folder(self, tmp_path: Path) -> None:
        service, store = _service(tmp_path)
        _write_collector(store, db="real_db", job=JOB)
        real_db = tmp_path / "real_db"
        os.symlink(real_db, tmp_path / DB)

        with pytest.raises(ValueError, match="escapes the artifact root"):
            service.delete_job_artifacts(DB, JOB)

        assert (real_db / JOB).exists()

    def test_rejects_a_meta_marker_symlinked_to_a_file_inside_the_root(
        self, tmp_path: Path
    ) -> None:
        service, store = _service(tmp_path)
        _write_collector(store, db="other_db", job="job-002")
        store.write_json("other_db/job-002/secret.json", {"keep": True})
        real_secret = tmp_path / "other_db" / "job-002" / "secret.json"
        (tmp_path / ".meta").mkdir(parents=True, exist_ok=True)
        os.symlink(real_secret, tmp_path / ".meta" / f"{JOB}.json")

        with pytest.raises(ValueError, match="escapes the artifact root"):
            service.delete_job_artifacts(DB, JOB)

        assert real_secret.exists()
        assert real_secret.read_text() == '{\n  "keep": true\n}'

    def test_rejects_a_symlinked_meta_directory(
        self, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """S-c: the .meta *directory* itself is a symlink -- distinct from
        the marker-file-is-a-symlink case above. _resolve_meta_path's
        os.path.islink(meta_dir) check must catch this on its own."""
        service, store = _service(tmp_path)
        _write_collector(store)
        outside = tmp_path_factory.mktemp("outside-meta-dir")
        (outside / f"{JOB}.json").write_text('{"leaked": true}')
        os.symlink(outside, tmp_path / ".meta")

        with pytest.raises(ValueError, match="escapes the artifact root"):
            service.delete_job_artifacts(DB, JOB)

        assert (outside / f"{JOB}.json").exists()
        assert (tmp_path / DB / JOB).exists()

    def test_leaves_an_internal_symlink_pointing_outside_intact(
        self, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """A legitimate job folder containing, as one of its own artifact
        entries, a symlink to something outside the root. The job folder
        itself isn't a symlink, so deletion proceeds -- but the external
        target must be left exactly as it was (shutil.rmtree never follows
        a symlink into its target, only removes the link entry itself)."""
        service, store = _service(tmp_path)
        _write_collector(store)
        outside = tmp_path_factory.mktemp("outside-internal-link")
        target = outside / "untouched.json"
        target.write_text("{}")
        os.symlink(target, tmp_path / DB / JOB / "evil_link")

        service.delete_job_artifacts(DB, JOB)

        assert not (tmp_path / DB / JOB).exists()
        # The link entry itself is gone (it was part of the job folder),
        # but shutil.rmtree only ever unlinks a symlink it encounters --
        # it never follows one into its target -- so the external file the
        # link pointed at is completely untouched.
        assert target.exists()
        assert target.read_text() == "{}"

    def test_artifact_root_itself_being_a_symlink_still_works(
        self, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """ARTIFACT_DIR pointing at a symlink is normal (e.g. /tmp on
        macOS) -- root_realpath resolves it once, consistently, and
        deletion still succeeds against the real location."""
        real_root = tmp_path_factory.mktemp("real-artifact-root")
        link_parent = tmp_path_factory.mktemp("link-parent")
        artifact_dir = link_parent / "artifacts"
        os.symlink(real_root, artifact_dir)

        service, store = _service(artifact_dir)
        _write_collector(store)

        removed = service.delete_job_artifacts(DB, JOB)

        assert removed == [f"{DB}/{JOB}/collector/output.json"]
        assert not (real_root / DB / JOB).exists()


# ---------------------------------------------------------------------------
# delete_job_artifacts -- .trash/ safety (round-2 review, A) and sweeping
# (S-a)
# ---------------------------------------------------------------------------


class TestTrashDirSafety:
    def test_rejects_a_symlinked_trash_dir_and_touches_nothing_outside(
        self, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """.trash/ is a symlink to somewhere outside the root -- os.rename
        would move the job folder's contents there. Refused before the
        rename ever happens (round-2 review, A); nothing outside is
        touched, and the job folder is still exactly where it started."""
        outside = tmp_path_factory.mktemp("outside-trash")
        service, store = _service(tmp_path)
        _write_collector(store)
        os.symlink(outside, tmp_path / ".trash")

        with pytest.raises(ValueError, match="escapes the artifact root"):
            service.delete_job_artifacts(DB, JOB)

        assert list(outside.iterdir()) == []
        assert (tmp_path / DB / JOB / "collector" / "output.json").exists()

    def test_sweeps_an_old_tombstone_left_by_a_previous_failed_cleanup(
        self, tmp_path: Path
    ) -> None:
        """S-a: a tombstone older than an hour is cleared at the start of
        the next delete call."""
        service, store = _service(tmp_path)
        _write_collector(store, db=DB, job="job-002")
        trash_dir = tmp_path / ".trash"
        old_tombstone = trash_dir / "wordpress-job-999-deadbeef"
        old_tombstone.mkdir(parents=True)
        (old_tombstone / "leftover.json").write_text("{}")
        old_time = time.time() - 7200
        os.utime(old_tombstone / "leftover.json", (old_time, old_time))
        os.utime(old_tombstone, (old_time, old_time))

        service.delete_job_artifacts(DB, "job-002")

        assert not old_tombstone.exists()

    def test_does_not_sweep_a_recent_tombstone(self, tmp_path: Path) -> None:
        service, store = _service(tmp_path)
        _write_collector(store, db=DB, job="job-002")
        trash_dir = tmp_path / ".trash"
        recent_tombstone = trash_dir / "wordpress-job-999-cafef00d"
        recent_tombstone.mkdir(parents=True)
        (recent_tombstone / "leftover.json").write_text("{}")

        service.delete_job_artifacts(DB, "job-002")

        assert recent_tombstone.exists()

    def test_sweep_is_bounded_per_call(self, tmp_path: Path) -> None:
        service, store = _service(tmp_path)
        _write_collector(store, db=DB, job="job-002")
        trash_dir = tmp_path / ".trash"
        old_time = time.time() - 7200
        tombstones = []
        for i in range(8):
            tombstone = trash_dir / f"wordpress-job-old-{i}"
            tombstone.mkdir(parents=True)
            os.utime(tombstone, (old_time, old_time))
            tombstones.append(tombstone)

        service.delete_job_artifacts(DB, "job-002")

        remaining = [t for t in tombstones if t.exists()]
        assert len(remaining) == len(tombstones) - 5

    def test_sweep_removes_an_old_symlink_entry_without_following_it(
        self, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """A tombstone entry under .trash/ can itself be a dangling or
        stray symlink (e.g. a job folder that contained one, swept up
        along with everything else by a previous delete's os.rename). The
        sweep must remove the link entry -- it's old -- without ever
        following it into its target."""
        service, store = _service(tmp_path)
        _write_collector(store, db=DB, job="job-002")
        outside = tmp_path_factory.mktemp("outside-sweep-link")
        target = outside / "untouched.json"
        target.write_text("{}")

        trash_dir = tmp_path / ".trash"
        trash_dir.mkdir(parents=True)
        link_entry = trash_dir / "wordpress-job-old-link"
        os.symlink(target, link_entry)
        old_time = time.time() - 7200
        os.utime(link_entry, (old_time, old_time), follow_symlinks=False)

        service.delete_job_artifacts(DB, "job-002")

        assert not link_entry.exists() and not link_entry.is_symlink()
        assert target.exists()
        assert target.read_text() == "{}"

    def test_sweep_does_nothing_when_trash_dir_itself_is_a_symlink(
        self, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """.trash/ itself is a symlink to an outside directory containing
        an old-looking entry -- the sweep must never scan through it (it
        isn't the delete's own validated .trash/, so sweeping it would be
        scanning, and potentially deleting from, an attacker-chosen
        location)."""
        outside = tmp_path_factory.mktemp("outside-trash-sweep")
        old_entry = outside / "old-looking"
        old_entry.mkdir()
        old_time = time.time() - 7200
        os.utime(old_entry, (old_time, old_time))

        service, store = _service(tmp_path)
        _write_collector(store, db=DB, job="job-002")
        os.symlink(outside, tmp_path / ".trash")

        # The main delete flow for job-002 would separately refuse the
        # symlinked .trash/ (round-2 review, A) -- what this test checks
        # is that the sweep itself, which runs first, never touches
        # `outside` either way.
        with pytest.raises(ValueError, match="escapes the artifact root"):
            service.delete_job_artifacts(DB, "job-002")

        assert old_entry.exists()


# ---------------------------------------------------------------------------
# delete_job_artifacts -- partial-failure accounting (I1)
# ---------------------------------------------------------------------------


class TestPartialFailure:
    def test_meta_removal_failure_is_reported_as_partial_with_job_folder_removed(
        self, tmp_path: Path
    ) -> None:
        service, store = _service(tmp_path)
        _write_collector(store)
        store.write_json(f".meta/{JOB}.json", {"job_id": JOB, "phases": {}})

        with patch("os.remove", side_effect=OSError("boom")):
            with pytest.raises(ArtifactDeletionError) as exc_info:
                service.delete_job_artifacts(DB, JOB)

        assert exc_info.value.removed == [f"{DB}/{JOB}/collector/output.json"]
        assert not (tmp_path / DB / JOB).exists()

    def test_rename_failure_removes_nothing_and_reports_empty(self, tmp_path: Path) -> None:
        """Round-2 review, C: a failed rename must not touch .meta either,
        and must undo the CANCELLED marker it wrote in anticipation of a
        successful delete -- a failed delete leaves the job exactly as it
        found it, status included."""
        service, store = _service(tmp_path)
        _write_collector(store)
        store.write_json(f".meta/{JOB}.json", {"job_id": JOB, "phases": {}})
        status_before = service.job_status(DB, JOB)

        with patch("os.rename", side_effect=OSError("boom")):
            with pytest.raises(ArtifactDeletionError) as exc_info:
                service.delete_job_artifacts(DB, JOB)

        assert exc_info.value.removed == []
        # Nothing was renamed away -- the job folder is still exactly
        # where it started, and so is .meta.
        assert (tmp_path / DB / JOB / "collector" / "output.json").exists()
        assert (tmp_path / ".meta" / f"{JOB}.json").exists()
        # The CANCELLED marker written in anticipation of a successful
        # delete was removed again, so the job's status is unchanged.
        assert not (tmp_path / DB / JOB / "_status.json").exists()
        assert service.job_status(DB, JOB) == status_before

    def test_rename_failure_restores_a_pre_existing_markers_original_bytes(
        self, tmp_path: Path
    ) -> None:
        """Round-3 review: if the job already had its own _status.json
        before this call (e.g. an earlier, unrelated cancellation), a
        failed rename must restore its exact original bytes -- not just
        leave the CANCELLED marker this method wrote in anticipation of
        success, which would silently overwrite that marker's own
        stopped_at/last_phase even though the delete failed."""
        service, store = _service(tmp_path)
        _write_collector(store)
        original_marker_path = tmp_path / DB / JOB / "_status.json"
        original_bytes = b'{\n  "status": "CANCELLED",\n  "stopped_at": "2026-01-01T00:00:00+00:00",\n  "last_phase": "RunCollector"\n}'
        original_marker_path.write_bytes(original_bytes)

        with patch("os.rename", side_effect=OSError("boom")):
            with pytest.raises(ArtifactDeletionError):
                service.delete_job_artifacts(DB, JOB)

        assert original_marker_path.read_bytes() == original_bytes
