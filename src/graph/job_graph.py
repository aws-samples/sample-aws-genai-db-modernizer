# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Build a job's context graph and tell whether a saved one is current (#307).

Shared by the API (``src/api/routes/graph.py``) and the command line
(``scripts/graph_query.py``):

- ``is_current_graph_file``: a saved graph is used only when it carries this
  code's schema revision and, when known, the job's effective assignment
  version and the digest of its input artifacts (ADR-023, #819, #847).
- ``build_graph_file``: build into a temporary folder of this build's own next
  to ``path``, on a brand-new handle, then move it over ``path``, so a failed or interrupted build leaves the
  previous graph intact; a complete build is uploaded when a persistence
  layer is given.
- ``prepare_graph_file``: local file, then a download, then a current CLI
  copy, then a build.
- ``open_job_graph``: the command line's read-only way in. It reads the API's
  graph when that is current. Otherwise it builds its own copy
  (``graph/cli/context.lbug``) under a file lock and reads that, so it never
  writes the file the API serves: the API rebuilds its own file (temporary
  file, then an atomic rename) without knowing about the CLI, and two CLI
  processes build the copy once between them. When the API's file is current
  again, the copy is removed.
- ``prebuild``: build the job's graph at the end of a run (``scripts/run_report.py``,
  #1911) through ``open_job_graph``, so the copy is ready before the results
  page asks for it.
- ``adopt_cli_copy``: the API's way to use that copy. When the API has no
  current file (a job's first read, or an open graph behind a re-run's
  artifacts), it copies a current CLI copy over its own file (temporary file,
  then an atomic rename) instead of building, so the first read of a finished
  job opens a file. The API stays the only writer of its file.

Reading the API's file while the API swaps in a new one relies on POSIX rename
semantics: an open handle keeps the old file. On Windows, where a file another
process holds open cannot be replaced, the command line never reads the API's
file and always uses its own copy. The lock is ``fcntl.flock`` on POSIX and
``msvcrt.locking`` (exclusive only) on Windows, imported only when the CLI
takes it, so importing this module (as the API does) needs neither.

The API keeps its per-job caches, back-off and in-process locks; these
functions hold no state.
"""

from __future__ import annotations

import errno
import logging
import os
import shutil
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol

from src.graph import GraphStoreCache
from src.graph import populators as _populators
from src.graph import schema as _schema
from src.graph.inputs_digest import graph_inputs_digest
from src.graph.schema import GRAPH_SCHEMA_REVISION
from src.graph.store import READ_QUERY_TIMEOUT_MS, GraphStore, ReadLimits
from src.storage.artifact_store import ArtifactStore
from src.storage.assignment_versioning import resolve_effective_assignment_version

logger = logging.getLogger(__name__)

#: The CLI's own copy of a job's graph, next to the API's ``graph/context.lbug``.
CLI_GRAPH_DIR = "cli"

#: Prefix of a build's own temporary folder next to the graph file.
_BUILDING_PREFIX = ".building-"

#: True on Windows: no POSIX rename semantics, so the CLI never reads the API's file.
IS_WINDOWS = os.name == "nt"


class _Uploader(Protocol):
    def upload(self, db_name: str, job_id: str, path: str) -> Any: ...


def api_graph_path(artifact_root: str, db_name: str, job_id: str) -> str:
    """The graph file the API serves for this job; ValueError when it leaves the root."""
    return GraphStoreCache(base_dir=artifact_root).local_path(db_name, job_id)


def cli_graph_path(artifact_root: str, db_name: str, job_id: str) -> str:
    """The command line's own copy of the job's graph; ValueError when it leaves the root."""
    return _cli_copy_of(api_graph_path(artifact_root, db_name, job_id))


def _cli_copy_of(api_path: str) -> str:
    """The CLI copy next to the API's graph file ``api_path``; ValueError on a symlink."""
    path = os.path.join(os.path.dirname(api_path), CLI_GRAPH_DIR, "context.lbug")
    if os.path.realpath(path) != path:
        raise ValueError("graph path escapes the artifact root")
    return path


def sidecars(path: str) -> tuple[str, str, str]:
    """The database file and the engine's write-ahead-log and shadow files."""
    return path, f"{path}.wal", f"{path}.shadow"


def inputs_digest(artifact_store: ArtifactStore, db_name: str, job_id: str) -> str:
    """The digest of the job's graph input artifacts (#847)."""
    return graph_inputs_digest(artifact_store, db_name, job_id)


def is_current_graph_file(
    path: str, effective_version: int | None = None, inputs_digest: str | None = None
) -> bool:
    """True when ``path`` holds a readable, current graph with at least one node.

    Current means built with this code's schema revision (``GraphMeta``): a
    graph saved by an earlier release (no MOVED table, say) is stale, so the
    caller rebuilds it from the artifacts instead of serving it (ADR-023).
    When ``effective_version`` is given, the graph must also have been built
    from that assignment version: a graph saved before a customer edit or any
    later assignment version is stale too (#819). When ``inputs_digest`` is
    given, it must match the digest of the artifacts the graph was built from:
    a phase re-run on the same version rewrites artifacts without a new
    version (#847).
    """
    if not Path(path).exists():
        return False
    try:
        probe = GraphStore(path, read_only=True)
    except Exception as exc:  # corrupt/unreadable file -> caller rebuilds
        logger.warning("graph at %s unusable: %s", path, exc)
        return False
    try:
        if not probe.is_populated():
            return False
        meta = probe.graph_meta()
        revision = meta["schema_revision"]
        if revision != GRAPH_SCHEMA_REVISION:
            logger.info(
                "graph at %s has schema revision %s, not %s: rebuilding",
                path,
                revision,
                GRAPH_SCHEMA_REVISION,
            )
            return False
        if effective_version is not None and meta["effective_version"] != effective_version:
            logger.info(
                "graph at %s was built from assignment v%s, not v%s: rebuilding",
                path,
                meta["effective_version"],
                effective_version,
            )
            return False
        if inputs_digest is not None and meta["inputs_digest"] != inputs_digest:
            logger.info("graph at %s was built from older artifacts: rebuilding", path)
            return False
        return True
    finally:
        probe.close()


def build_graph_file(
    db_name: str,
    job_id: str,
    path: str,
    artifact_store: ArtifactStore,
    persistence: _Uploader | None = None,
    *,
    rebuild: Callable[..., dict] | None = None,
    init_schema: Callable[[GraphStore], None] | None = None,
    on_built: Callable[[bool], None] | None = None,
) -> dict:
    """Rebuild the graph into a temporary file, move it over ``path``, then upload it.

    The caller must hold exclusive access to ``path`` in its own process. The
    build goes to a temporary folder no other build shares (another process
    building the same job, #1911), next to the live file, and is renamed into
    place, so a failed or interrupted build leaves the previous graph intact,
    and the file never accumulates the history of earlier builds. Closing the writer before the rename
    checkpoints the new file. ``on_built(complete)`` runs as soon as the build
    is known to be complete or not, before the file is moved; ``rebuild`` and
    ``init_schema`` default to the populators' and the schema's.
    """
    rebuild = rebuild or _populators.rebuild_graph
    init_schema = init_schema or _schema.initialize_schema
    folder = tempfile.mkdtemp(dir=os.path.dirname(path), prefix=_BUILDING_PREFIX)
    building = os.path.join(folder, os.path.basename(path))
    try:
        # Always build on a brand-new handle. Re-running the same parameterised
        # inserts on one connection after the schema is dropped and recreated makes
        # the engine silently insert nothing, so a reused writer would produce an
        # empty graph.
        writer = GraphStore(building)
        try:
            init_schema(writer)
            stats = rebuild(db_name, job_id, artifact_store, writer)
        finally:
            writer.close()
        complete = stats.get("complete", True)
        if on_built is not None:
            on_built(complete)
        if not complete and is_current_graph_file(path):
            # An artifact could not be read: keep the previous graph (and its
            # stamp), so a later read retries instead of serving a partial one.
            logger.warning(
                "graph rebuild for %s/%s was incomplete; keeping the previous graph",
                db_name,
                job_id,
            )
            return stats
        for leftover in sidecars(path)[1:]:
            Path(leftover).unlink(missing_ok=True)
        os.replace(building, path)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    if not complete or persistence is None:
        # No previous graph: serve this unstamped one, which a later read
        # rebuilds; never upload it for other API tasks to reuse.
        return stats
    try:
        persistence.upload(db_name, job_id, path)
    except Exception as exc:  # upload failure must not break the response
        logger.warning("graph upload failed for %s/%s: %s", db_name, job_id, exc)
    return stats


def prepare_graph_file(
    path: str,
    *,
    version: int | None,
    digest: str | None,
    download: Callable[[str], bool],
    build: Callable[[], object],
    adopt: Callable[[str], bool] | None = None,
) -> None:
    """Make ``path`` hold a current graph: the local file, else a download, else an
    adopted CLI copy, else a build.

    A local, downloaded or adopted graph is used only when it is current for
    ``version`` and ``digest`` (None: not checked); ``download(path)`` and
    ``adopt(path)`` return True when they wrote the file.
    """
    if is_current_graph_file(path, version, digest):
        return
    if download(path) and is_current_graph_file(path, version, digest):
        return
    if adopt is not None and adopt(path) and is_current_graph_file(path, version, digest):
        return
    build()


def adopt_cli_copy(path: str, version: int | None, digest: str | None) -> bool:
    """Copy a current CLI copy of the job's graph over the API's file ``path`` (#1911).

    True when it did. The copy is checked and read under its shared lock, so
    the copy is neither rebuilt nor removed while it is read: a CLI build or
    ``remove_cli_copy`` holds the exclusive lock, and this waits for it. It is
    copied to a temporary file of its own next to ``path`` (another process
    adopting the same job has its own) and renamed into place. No copy, a stale
    one (another assignment version, older artifacts, an earlier schema
    revision) or a symlinked CLI folder is not adopted, and ``path`` is left as
    it was. Neither is a copy that cannot be read or written (a lock file
    another user owns, a full disk, a file Windows will not replace): that is
    logged, and the caller builds as before.

    Once adopted, the copy is removed when no CLI run is reading it, so the job
    keeps one graph: the CLI reads the API's file while that is current. Not on
    Windows, where the CLI never reads the API's file and keeps its copy.
    """
    try:
        cli = _cli_copy_of(path)
    except ValueError as exc:
        logger.warning("CLI graph copy refused: %s", exc)
        return False
    if not Path(cli).exists():
        return False
    adopting: str | None = None
    try:
        with _file_lock(cli) as lock:
            lock.acquire("shared")
            if not is_current_graph_file(cli, version, digest):
                return False
            fd, adopting = tempfile.mkstemp(
                dir=os.path.dirname(path), prefix=f"{os.path.basename(path)}.adopting."
            )
            os.close(fd)
            shutil.copyfile(cli, adopting)
        for leftover in sidecars(path)[1:]:
            Path(leftover).unlink(missing_ok=True)
        os.replace(adopting, path)
    except OSError as exc:
        logger.warning("could not adopt the CLI graph copy %s, building instead: %s", cli, exc)
        return False
    finally:
        if adopting is not None:
            Path(adopting).unlink(missing_ok=True)
    logger.info("adopted the prebuilt graph %s", cli)
    if not IS_WINDOWS:
        _drop_adopted_copy(cli)
    return True


def _drop_adopted_copy(cli: str) -> None:
    """Remove the CLI copy the API just adopted, unless a CLI run holds it; never raises."""
    try:
        _remove_copy(cli, wait=False)
    except OSError as exc:
        logger.warning("could not remove the adopted CLI graph copy %s: %s", cli, exc)


class _FileLock:
    """A lock on ``<path>.lock`` held by this process: shared or exclusive.

    POSIX: ``fcntl.flock``. Windows: ``msvcrt.locking`` on the first byte,
    exclusive for both modes. Either module is imported here, never at module
    import. The lock file is opened with ``O_NOFOLLOW`` where the platform has
    it, so a symlink planted at its path is refused rather than followed.
    """

    def __init__(self, path: str) -> None:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        self.fd = os.open(f"{path}.lock", flags, 0o600)
        self.mode: str | None = None

    def acquire(self, mode: str) -> None:
        """Take the lock in ``mode`` (``"shared"`` or ``"exclusive"``), waiting for it."""
        self.release()
        if IS_WINDOWS:
            import msvcrt

            while True:
                try:
                    msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
                    break
                except OSError:
                    time.sleep(0.05)
        else:
            import fcntl

            fcntl.flock(self.fd, fcntl.LOCK_SH if mode == "shared" else fcntl.LOCK_EX)
        self.mode = mode

    def release(self) -> None:
        if self.mode is None:
            return
        if IS_WINDOWS:
            import msvcrt

            os.lseek(self.fd, 0, os.SEEK_SET)
            msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        else:
            import fcntl

            fcntl.flock(self.fd, fcntl.LOCK_UN)
        self.mode = None

    def close(self) -> None:
        self.release()
        os.close(self.fd)


@contextmanager
def _file_lock(path: str) -> Iterator[_FileLock]:
    lock = _FileLock(path)
    try:
        yield lock
    finally:
        lock.close()


def freshness(
    artifact_store: ArtifactStore, db_name: str, job_id: str
) -> tuple[int | None, str | None]:
    """The job's effective assignment version and inputs digest, by the route's rule.

    The version is 0 for a job with no assignment yet. A version that cannot be
    read is None, and then the digest is not computed either; a digest that
    cannot be read is None. A None is not checked, so a graph of the current
    schema revision is used as it is, as the API does (#819, #847).
    """
    try:
        version = resolve_effective_assignment_version(artifact_store, db_name, job_id)
    except Exception as exc:  # noqa: BLE001 - never fail a read on the freshness check
        logger.warning("could not read the effective assignment version: %s", exc)
        return None, None
    try:
        return version, inputs_digest(artifact_store, db_name, job_id)
    except Exception as exc:  # noqa: BLE001 - never fail a read on the freshness check
        logger.warning("could not digest the graph inputs: %s", exc)
        return version, None


def remove_cli_copy(artifact_root: str, db_name: str, job_id: str, *, wait: bool) -> bool:
    """Remove the CLI's copy of the job's graph; True when nothing is left of it.

    Taken under the exclusive lock, so a CLI run reading the copy is never cut
    off: with ``wait`` False, a busy copy is left for a later run. The lock
    file stays (removing it would let two runs lock different files).
    """
    return _remove_copy(cli_graph_path(artifact_root, db_name, job_id), wait=wait)


def _remove_copy(cli: str, *, wait: bool) -> bool:
    if not Path(cli).exists():
        return True
    lock = _FileLock(cli)
    try:
        if wait:
            lock.acquire("exclusive")
        elif not _try_exclusive(lock):
            return False
        for leftover in (*sidecars(cli), f"{cli}.building"):
            Path(leftover).unlink(missing_ok=True)
        return True
    finally:
        lock.close()


def _try_exclusive(lock: _FileLock) -> bool:
    if IS_WINDOWS:
        import msvcrt

        try:
            msvcrt.locking(lock.fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
        except OSError:
            return False
    else:
        import fcntl

        try:
            fcntl.flock(lock.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                return False
            raise
    lock.mode = "exclusive"
    return True


def remove_cli_dir(artifact_root: str, db_name: str, job_id: str) -> None:
    """Remove ``graph/cli/`` (copy and lock file): only when no CLI run is active."""
    cli_dir = os.path.dirname(cli_graph_path(artifact_root, db_name, job_id))
    if remove_cli_copy(artifact_root, db_name, job_id, wait=True):
        shutil.rmtree(cli_dir, ignore_errors=True)


def _open_read_only(path: str) -> GraphStore:
    return GraphStore(
        path,
        read_only=True,
        query_timeout_ms=READ_QUERY_TIMEOUT_MS,
        read_limits=ReadLimits.from_env(),
    )


@contextmanager
def open_job_graph(
    artifact_store: ArtifactStore, artifact_root: str, db_name: str, job_id: str
) -> Iterator[tuple[GraphStore, dict]]:
    """A read-only handle on a current graph of the job, and where it came from.

    Yields ``(store, info)``; ``info["source"]`` is ``"api"`` when the API's
    graph was current and read as it is (never on Windows), ``"cli"`` for the
    command line's own copy, and ``info["rebuilt"]`` says whether that copy
    was built on this call. The handle carries the read timeout and read limits.

    The copy is read under a shared lock, so CLI reads run side by side. Only
    a stale copy takes the exclusive lock, and every step rechecks freshness
    after it gets its lock: the shared-to-exclusive switch is not atomic (on
    Linux and BSD it releases first), so another run may build in between, and
    the recheck then finds the copy current.
    """
    api = api_graph_path(artifact_root, db_name, job_id)
    cli = cli_graph_path(artifact_root, db_name, job_id)
    version, digest = freshness(artifact_store, db_name, job_id)
    if not IS_WINDOWS and is_current_graph_file(api, version, digest):
        try:
            store = _open_read_only(api)
        except Exception as exc:  # noqa: BLE001 - replaced or locked mid-open: use the copy
            logger.info("could not open the API graph %s, using the CLI copy: %s", api, exc)
        else:
            try:
                yield store, {"source": "api", "rebuilt": False, "path": api}
            finally:
                store.close()
            # The API's graph serves this job again: the copy is no longer needed.
            remove_cli_copy(artifact_root, db_name, job_id, wait=False)
            return
    Path(cli).parent.mkdir(parents=True, exist_ok=True)
    info: dict = {"source": "cli", "rebuilt": False, "path": cli}
    with _file_lock(cli) as lock:
        lock.acquire("shared")
        if not is_current_graph_file(cli, version, digest):
            lock.acquire("exclusive")
            if not is_current_graph_file(cli, version, digest):
                stats = build_graph_file(db_name, job_id, cli, artifact_store)
                info["rebuilt"] = True
                if not stats.get("complete", True):
                    info["complete"] = False
            lock.acquire("shared")
        store = _open_read_only(cli)
        try:
            yield store, info
        finally:
            store.close()


def prebuild(artifact_store: ArtifactStore, artifact_root: str, db_name: str, job_id: str) -> dict:
    """Build the job's graph ahead of its first read (#1911); ``open_job_graph``'s info.

    The CLI copy is built unless the API's graph is already current, which is
    then left as it is. The API adopts the copy (``adopt_cli_copy``) on its
    first read instead of building.
    """
    with open_job_graph(artifact_store, artifact_root, db_name, job_id) as (_, info):
        return info
