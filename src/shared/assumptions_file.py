# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The optional assumptions file (#1991): every assumption a customer can set, in one place.

A chat or headless run passes it as ``run_assessment.py --assumptions <file>`` (and
``/modernize --assumptions <file>``). It is one JSON object whose keys are the
assumptions block's own (``src.shared.assumptions``)::

    {"growth": 3, "peak": 2.5}

- ``growth``: the growth multiple of the scenario line (default 5×), above 0 and at
  most 100. Never applied to a design rate, a risk or the headline cost.
- ``peak``: the peak-to-average ratio the DynamoDB, DocumentDB and OpenSearch design
  rates take, from 1 to 100, in place of the one the source's metrics measured (or "not
  measured"). The Aurora and ElastiCache designs take no peak.

The other items of the block are collected, not assumed: the rate window and its check
come from the collection, the region and price date from the price table. A file that
names one is refused, as is an unknown key, a value that is not a finite number in range,
a duplicate key, or anything but one JSON object. The file's text is customer text: it is
never recorded or put in a prompt, and a refusal names a key JSON-escaped and cut short.
Only the checked numbers, and the file's SHA-256, are recorded (``record_assumptions``).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from src.shared.assumptions import SETTABLE, check_value, range_text

#: The file's keys, by the key each is recorded under.
FILE_KEYS: dict[str, str] = {name: key for key, (name, *_) in SETTABLE.items()}
#: The assumptions block's items no file can set, with why.
COLLECTED_KEYS = ("rate_window", "rate_window_check", "pricing")
#: A file this large is not an assumptions file.
MAX_FILE_BYTES = 16 * 1024
_SHOWN_KEY_CHARS = 40


class AssumptionsFileError(ValueError):
    """Why an assumptions file is refused; the message never carries the file's text."""


@dataclass(frozen=True)
class AssumptionsFile:
    """The checked values, by recorded key, and the file's SHA-256."""

    values: dict[str, float]
    sha256: str


def _shown(key: str) -> str:
    text = json.dumps(key[:_SHOWN_KEY_CHARS])
    return text if len(key) <= _SHOWN_KEY_CHARS else text[:-1] + '..."'


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    seen: dict[str, object] = {}
    for key, value in pairs:
        if key in seen:
            raise AssumptionsFileError(f"key {_shown(key)} is set more than once")
        seen[key] = value
    return seen


class _NotFinite:
    """A ``NaN`` or ``Infinity`` literal: refused as a value, never parsed to a float."""


def read_assumptions_file(path: str | Path) -> AssumptionsFile:
    """The assumptions ``path`` sets, checked; AssumptionsFileError saying why if not."""
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_FILE_BYTES + 1)
    except OSError as exc:
        raise AssumptionsFileError(
            f"the file cannot be read ({exc.strerror or type(exc).__name__})"
        ) from None
    return parse_assumptions(raw)


def parse_assumptions(
    raw: bytes, *, subject: str = "the file", echo: bool = True
) -> AssumptionsFile:
    """The assumptions ``raw`` (an assumptions file's bytes) sets, checked, by the file's
    rules: the one check the file and the UI's settings panel (``subject`` "the request",
    #1991) share. AssumptionsFileError saying why if not; it never carries ``raw``.
    With ``echo`` False (the API, #1991 review) a refusal names only the field, never a
    key or a value the caller sent."""
    if len(raw) > MAX_FILE_BYTES:
        raise AssumptionsFileError(f"{subject} is larger than {MAX_FILE_BYTES // 1024} KiB")
    try:
        # utf-8-sig: an editor's byte order mark (Notepad, PowerShell) is no character.
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise AssumptionsFileError(f"{subject} is not UTF-8 text") from None
    try:
        data = json.loads(
            text, object_pairs_hook=_no_duplicates, parse_constant=lambda _: _NotFinite()
        )
    except AssumptionsFileError:
        if not echo:
            raise AssumptionsFileError(f"{subject} sets a key more than once") from None
        raise
    except ValueError as exc:
        raise AssumptionsFileError(
            f"{subject} is not valid JSON (line {getattr(exc, 'lineno', '?')})"
        ) from None
    if not isinstance(data, dict):
        raise AssumptionsFileError(f'{subject} must hold a JSON object, such as {{"growth": 3}}')
    if not data:
        raise AssumptionsFileError(
            f"{subject} sets no assumption (it may set: {', '.join(FILE_KEYS)})"
        )
    values: dict[str, float] = {}
    for name, value in data.items():
        if name in COLLECTED_KEYS:
            raise AssumptionsFileError(
                f"{name} is collected from the source or the price table, not set by "
                f"{subject} (it may set: {', '.join(FILE_KEYS)})"
            )
        if name not in FILE_KEYS:
            what = f"unknown key {_shown(name)}" if echo else f"{subject} has an unknown key"
            raise AssumptionsFileError(f"{what} (it may set: {', '.join(FILE_KEYS)})")
        if isinstance(value, _NotFinite):
            value = float("nan")
        try:
            values[FILE_KEYS[name]] = check_value(FILE_KEYS[name], value)
        except ValueError as exc:
            if not echo:
                raise AssumptionsFileError(
                    f"{name} must be a number {range_text(FILE_KEYS[name])}"
                ) from None
            raise AssumptionsFileError(str(exc)) from None
    return AssumptionsFile(values=values, sha256=hashlib.sha256(raw).hexdigest())


__all__ = [
    "COLLECTED_KEYS",
    "FILE_KEYS",
    "MAX_FILE_BYTES",
    "AssumptionsFile",
    "AssumptionsFileError",
    "parse_assumptions",
    "read_assumptions_file",
]
