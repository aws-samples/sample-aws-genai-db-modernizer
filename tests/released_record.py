# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The public ``assess()`` contract's released-version record, as tests read it.

``src/assessment/released_contract.json`` names the newest contract version a release
recorded: the version a request naming none gets. The release step writes it after the
tag (CONTRIBUTING.md, Releases, step 6), so tests that depend on the default read it
here rather than naming a version, and the record moves the default with no test edit:

- :data:`RECORDED_VERSION` is the version the record names, read from the file itself
  (not through the code under test);
- :func:`versions_note` is how a refusal names the versions this code reads, each one
  newer than the record marked "(read only when pinned)";
- :func:`recorded_as` puts a record of another version in place for one test, so both
  states (before and after a version is recorded) stay covered whatever the file says.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
RECORD_PATH = REPO / "src" / "assessment" / "released_contract.json"

#: The contract versions this code reads, oldest first. Kept here on purpose, not read
#: from the models: a version added to the code needs its tests updated with it.
SUPPORTED = ("1.3", "1.4", "1.5")

#: The version the record names (the default a request naming none gets).
RECORDED_VERSION: str = json.loads(RECORD_PATH.read_text(encoding="utf-8"))["contract_version"]
assert RECORDED_VERSION in SUPPORTED, f"the record names {RECORDED_VERSION!r}"


def _tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def versions_note(recorded: str = RECORDED_VERSION) -> str:
    """The versions a refusal names, as ``recorded`` leaves them: ``"1.3, 1.4 and 1.5"``
    once 1.5 is recorded, ``"1.3, 1.4 and 1.5 (read only when pinned)"`` before."""
    named = [
        f"{v} (read only when pinned)" if _tuple(v) > _tuple(recorded) else v for v in SUPPORTED
    ]
    return ", ".join(named[:-1]) + " and " + named[-1]


@contextmanager
def recorded_as(version: str, directory: Path, released: str = "v9.9.9") -> Iterator[Path]:
    """While open, the models read a record naming ``version`` (written in
    ``directory``), as if the release step had recorded it."""
    from src.assessment import models

    record = directory / "released_contract.json"
    record.write_text(
        json.dumps({"contract_version": version, "released": released}), encoding="utf-8"
    )
    models.released_contract_version.cache_clear()
    try:
        with patch.object(models, "RELEASED_VERSION_PATH", record):
            yield record
    finally:
        models.released_contract_version.cache_clear()
