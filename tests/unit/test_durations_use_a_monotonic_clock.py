# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""No code under src/ measures an interval with the wall clock (#2599).

``time.time()`` and ``datetime.now()`` step when the system clock is adjusted,
so an elapsed time or a deadline built on them can come out negative or too
long. Intervals use ``time.monotonic()``. The wall clock stays for timestamps
and for comparisons with a file's modification time or a stored timestamp,
which this scan does not flag.

The scan flags a subtraction or a comparison whose two sides are both clock
readings (a call, or a name assigned from one, or from one plus an offset),
unless both are monotonic: two wall-clock reads, or a wall-clock read against
a monotonic one. It resolves import aliases (``import time as t``,
``from time import time``, ``from datetime import datetime as dt``) and starts
bound by tuple unpacking or ``:=``.

Names are kept per file, not per scope: a ``start`` in two functions, or a
``self.start`` in two classes, share one entry. A name bound from both clocks
in one file is flagged wherever it meets a clock.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

WALL_TIME_FUNCS = {"time", "time_ns"}
MONOTONIC_TIME_FUNCS = {"monotonic", "monotonic_ns", "perf_counter", "perf_counter_ns"}
WALL_DATETIME_METHODS = {"now", "utcnow", "today"}


class _Clocks:
    """The names a module reads its clocks through, and the kind of each read."""

    def __init__(self, tree: ast.AST) -> None:
        self.time_modules: set[str] = set()
        self.datetime_modules: set[str] = set()
        self.datetime_classes: set[str] = set()
        self.functions: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "time":
                        self.time_modules.add(alias.asname or "time")
                    elif alias.name == "datetime":
                        self.datetime_modules.add(alias.asname or "datetime")
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    local = alias.asname or alias.name
                    if node.module == "time":
                        kind = self._time_kind(alias.name)
                        if kind:
                            self.functions[local] = kind
                    elif node.module == "datetime" and alias.name == "datetime":
                        self.datetime_classes.add(local)

    @staticmethod
    def _time_kind(name: str) -> str | None:
        if name in WALL_TIME_FUNCS:
            return "wall"
        if name in MONOTONIC_TIME_FUNCS:
            return "monotonic"
        return None

    def call_kind(self, node: ast.AST) -> str | None:
        if not isinstance(node, ast.Call):
            return None
        func = node.func
        if isinstance(func, ast.Name):
            return self.functions.get(func.id)
        if not isinstance(func, ast.Attribute):
            return None
        owner = func.value
        if isinstance(owner, ast.Name) and owner.id in self.time_modules:
            return self._time_kind(func.attr)
        if func.attr in WALL_DATETIME_METHODS:
            if isinstance(owner, ast.Name) and owner.id in self.datetime_classes:
                return "wall"
            if (
                isinstance(owner, ast.Attribute)
                and owner.attr == "datetime"
                and isinstance(owner.value, ast.Name)
                and owner.value.id in self.datetime_modules
            ):
                return "wall"
        return None

    def value_kind(self, node: ast.AST) -> str | None:
        """The clock a value reads: a clock call, or a clock call plus an offset."""
        kind = self.call_kind(node)
        if kind or not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add)):
            return kind
        kinds = {self.call_kind(node.left), self.call_kind(node.right)} - {None}
        return kinds.pop() if len(kinds) == 1 else None


def _interval_lines(source: str) -> list[int]:
    """Lines where an interval mixes clocks or uses the wall clock at both ends."""
    tree = ast.parse(source)
    clocks = _Clocks(tree)
    names: dict[str, set[str]] = {}

    def bind(target: ast.AST, value: ast.AST) -> None:
        if isinstance(target, (ast.Tuple, ast.List)):
            if isinstance(value, (ast.Tuple, ast.List)) and len(value.elts) == len(target.elts):
                for t, v in zip(target.elts, value.elts, strict=True):
                    bind(t, v)
        elif isinstance(target, (ast.Name, ast.Attribute)):
            kind = clocks.value_kind(value)
            if kind:
                names.setdefault(ast.unparse(target), set()).add(kind)

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                bind(target, node.value)
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
            bind(node.target, node.value)

    def kinds(node: ast.AST) -> set[str]:
        kind = clocks.call_kind(node)
        if kind:
            return {kind}
        if isinstance(node, (ast.Name, ast.Attribute)):
            return names.get(ast.unparse(node), set())
        return set()

    def wrong(left: ast.AST, right: ast.AST) -> bool:
        a, b = kinds(left), kinds(right)
        return bool(a and b) and not (a == b == {"monotonic"})

    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub):
            if wrong(node.left, node.right):
                lines.append(node.lineno)
        elif isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            if any(wrong(a, b) for a, b in zip(operands, operands[1:], strict=False)):
                lines.append(node.lineno)
    return sorted(lines)


@pytest.mark.passes_on_main  # a self-test of the scan in this file, not of src/
@pytest.mark.parametrize(
    "source",
    [
        "import time\nstart = time.time()\nelapsed = time.time() - start",
        "import time\nt0 = time.time()\nprint(time.time() - t0)",
        "from datetime import datetime\ns = datetime.now()\nd = (datetime.now() - s).total_seconds()",
        "import datetime\ns = datetime.datetime.utcnow()\nd = datetime.datetime.utcnow() - s",
        "import time\nclass T:\n    def a(self):\n        self.start = time.time()\n"
        "    def b(self):\n        return time.time() - self.start",
        "import time\ndeadline = time.time() + 60\nwhile time.time() < deadline:\n    pass",
        # the two clocks mixed
        "import time\nstart = time.time()\nelapsed = time.monotonic() - start",
        "import time\ndeadline = time.monotonic() + 60\nwhile time.time() < deadline:\n    pass",
        # aliases and other spellings
        "from time import time\nstart = time()\nelapsed = time() - start",
        "import time as t\nstart = t.time()\nelapsed = t.time() - start",
        "from datetime import datetime as dt\ns = dt.now()\nd = dt.now() - s",
        "import datetime as d\ns = d.datetime.now()\ne = d.datetime.now() - s",
        "from datetime import datetime\ns = datetime.today()\nd = datetime.today() - s",
        "import time\ns = time.time_ns()\nd = time.time_ns() - s",
        # starts bound by tuple unpacking or :=
        "import time\ns, x = time.time(), 1\nd = time.time() - s",
        "import time\nif (s := time.time()):\n    d = time.time() - s",
    ],
)
def test_the_scan_sees_a_wall_clock_interval(source: str) -> None:
    assert _interval_lines(source)


@pytest.mark.passes_on_main  # a self-test of the scan in this file, not of src/
@pytest.mark.parametrize(
    "source",
    [
        "import time\nstart = time.monotonic()\nelapsed = time.monotonic() - start",
        "import time\ndeadline = time.monotonic() + 60\nwhile time.monotonic() < deadline:\n    pass",
        "from time import monotonic as m\ns = m()\nd = m() - s",
        "import time\ns, x = time.monotonic(), 1\nd = time.monotonic() - s",
        "import time, os\nage = time.time() - os.stat('f').st_mtime",
        "import time\nnow = time.time()\nage = now - entry.stat().st_mtime",
        "from datetime import UTC, datetime, timedelta\nbase = datetime.now(UTC) - timedelta(hours=1)",
        "from datetime import UTC, datetime\nage = datetime.now(UTC) - parsed_when",
        "from datetime import datetime\nstamp = datetime.now()",
        "import time\nstamp = time.time_ns()",
    ],
)
def test_the_scan_ignores_timestamps_and_monotonic_intervals(source: str) -> None:
    assert not _interval_lines(source)


def test_no_src_code_measures_an_interval_with_the_wall_clock() -> None:
    offenders = {}
    for path in sorted((REPO / "src").rglob("*.py")):
        if "node_modules" in path.parts:
            continue  # the UI's third-party packages
        lines = _interval_lines(path.read_text(encoding="utf-8"))
        if lines:
            offenders[path.relative_to(REPO).as_posix()] = lines
    assert not offenders, offenders
