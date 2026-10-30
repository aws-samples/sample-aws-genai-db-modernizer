# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Resolve a sample collection path on a fresh clone (#331).

The bundled sample collections ship only as ``docs/examples/<name>/<name>.zip``
(``docs/examples/**/*.json`` is gitignored), so the documented first command
``--file docs/examples/wordpress/wordpress-collection.json`` would otherwise
fail with "file not found". :func:`resolve_collection_path` is the one place
that turns such a path into a readable ``.json`` file:

- an existing regular file that is not a zip archive is returned untouched,
  unless its tracked zip is newer, in which case it is re-extracted;
- a missing ``<name>-collection.json`` whose tracked ``<name>.zip`` (or
  ``<name>-collection.zip``) sits next to it is extracted into the same
  directory;
- a ``.zip`` passed directly is extracted the same way, into its own directory.

Only the one expected member is read, by exact name, and written to a path this
module computes; the archive's own member paths are never used as output paths,
so a zip-slip member (``../x.json``, an absolute path) can never be written.
The member is size-checked against the ingestion limit while it streams out.
"""

from __future__ import annotations

import os
import tempfile
import zipfile
from pathlib import Path

from src.tools.validation.ingestion_limits import (
    IngestionLimitError,
    IngestionLimits,
    check_file_size,
)

_SUFFIX = "-collection"
_CHUNK = 1024 * 1024


class CollectionPathError(ValueError):
    """The collection path cannot be resolved; the message is one actionable line."""


def _hint() -> str:
    return (
        "Pass a collector output .json file, or run the collection SQL script "
        "against your database (the bundled samples are docs/examples/<name>/<name>.zip)."
    )


def _is_zip(path: Path) -> bool:
    try:
        return zipfile.is_zipfile(path)
    except OSError:
        return False


def _current_umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask


def _extract_member(zip_path: Path, member: str, dest: Path) -> None:
    """Copy exactly ``member`` out of ``zip_path`` into ``dest`` (atomically)."""
    if member != Path(member).name or member in ("", ".", ".."):
        raise CollectionPathError(f"{member!r} is not a plain file name; {_hint()}")
    limits = IngestionLimits.from_env()
    try:
        with zipfile.ZipFile(zip_path) as zf:
            # Look the member up by exact name among the archive's entries; a
            # zip-slip name ('../x', '/x', 'a/x') can never equal a plain name.
            infos = [i for i in zf.infolist() if i.filename == member and not i.is_dir()]
            if not infos:
                raise CollectionPathError(
                    f"{zip_path} is a zip archive without {member}; unzip it and pass the .json "
                    f"file inside. {_hint()}"
                )
            info = infos[0]
            check_file_size(info.file_size, source=f"{zip_path}:{member}", limits=limits)
            dest.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=f".{member}.", suffix=".tmp")
            try:
                written = 0
                with os.fdopen(fd, "wb") as out, zf.open(info) as src:
                    while chunk := src.read(_CHUNK):
                        written += len(chunk)
                        check_file_size(written, source=f"{zip_path}:{member}", limits=limits)
                        out.write(chunk)
                os.chmod(tmp_name, 0o666 & ~_current_umask())
                os.replace(tmp_name, dest)
            finally:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
    except zipfile.BadZipFile as exc:
        raise CollectionPathError(f"{zip_path} is not a readable zip archive: {exc}") from exc
    except IngestionLimitError as exc:
        raise CollectionPathError(f"{zip_path}: {exc}") from exc
    except RuntimeError as exc:  # encrypted member ("password required")
        raise CollectionPathError(
            f"{zip_path}: cannot read {member} ({exc}); encrypted zip archives are not "
            f"supported. {_hint()}"
        ) from exc
    except NotImplementedError as exc:  # compression method zipfile lacks
        raise CollectionPathError(
            f"{zip_path}: cannot read {member} ({exc}); re-create the archive with deflate "
            f"or store compression. {_hint()}"
        ) from exc
    except OSError as exc:
        raise CollectionPathError(
            f"cannot write {dest} from {zip_path} ({exc.strerror or exc}); make the folder "
            f"writable or extract the .json yourself. {_hint()}"
        ) from exc


def _mtime(p: Path) -> float:
    return p.stat().st_mtime


def _sibling_zip(p: Path) -> Path | None:
    if p.suffix == ".json" and p.stem.endswith(_SUFFIX):
        base = p.stem[: -len(_SUFFIX)]
        for candidate in (p.with_name(f"{base}.zip"), p.with_name(f"{p.stem}.zip")):
            if candidate.is_file() and _is_zip(candidate):
                return candidate
    return None


def _refresh_if_stale(p: Path) -> None:
    """Re-extract ``p`` when its tracked zip is newer (e.g. after a pull updated the sample)."""
    candidate = _sibling_zip(p)
    if candidate is None or _mtime(candidate) <= _mtime(p):
        return
    try:
        with zipfile.ZipFile(candidate) as zf:
            if p.name not in zf.namelist():
                return  # an unrelated zip; leave the user's file alone
    except (zipfile.BadZipFile, OSError):
        return
    _extract_member(candidate, p.name, p)


def resolve_collection_path(path: str | os.PathLike[str]) -> str:
    """Return a path to a readable collection ``.json``, extracting a sample if needed.

    Raises:
        CollectionPathError: the path does not exist and no sibling zip provides it,
            or the zip does not contain the expected member.
    """
    p = Path(path)
    if p.is_file():
        if not _is_zip(p):
            _refresh_if_stale(p)
            return str(path)
        stem = p.stem
        member = stem if stem.endswith(_SUFFIX) else f"{stem}{_SUFFIX}"
        dest = p.with_name(f"{member}.json")
        if not dest.is_file() or _mtime(p) > _mtime(dest):
            _extract_member(p, dest.name, dest)
        return str(dest)

    candidate = _sibling_zip(p)
    if candidate is not None:
        _extract_member(candidate, p.name, p)
        return str(path)

    raise CollectionPathError(f"File not found: {path}. {_hint()}")
