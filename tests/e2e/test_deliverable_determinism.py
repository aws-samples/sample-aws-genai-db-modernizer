# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Two deterministic runs on the same input must give identical deliverables (#664).

Compares the session's discourse run with a second, independent run
(``--llm-mode none``, separate artifact root) over every rendered deliverable:
text files byte for byte, PDF page text. Only the
things that legitimately differ between runs are masked: timestamps, dates and
the job/artifact paths.
"""

from __future__ import annotations

import difflib
import io
import re
from pathlib import Path

import pytest
from pypdf import PdfReader

from tests.e2e.pipeline import PipelineResult, run_pipeline

pytestmark = pytest.mark.e2e

_MASKS = (
    (
        re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"),
        "<TS>",
    ),
    (re.compile(r"\d{4}-\d{2}-\d{2}"), "<DATE>"),
    (re.compile(r"\d{8}T?\d{0,6}Z?(?=\.)"), "<STAMP>"),
)


def _mask(text: str, run: PipelineResult) -> str:
    text = text.replace(str(run.artifact_root), "<ROOT>").replace(run.job_id, "<JOB>")
    text = text.replace(run.job_id[:8], "<JOB>")  # file names and titles truncate it
    for pattern, repl in _MASKS:
        text = pattern.sub(repl, text)
    return text


def _role(name: str, run: PipelineResult) -> str:
    """The deliverable's name without the job-id prefix and the UTC date stamp."""
    name = re.sub(r"_\d{8}(?=\.\w+$)", "", name)
    return name.replace(f"_{run.job_id[:8]}", "")


def _views(run: PipelineResult) -> dict[str, list[tuple[str, str]]]:
    """role -> comparable (part name, masked text) pairs for every deliverable."""
    out: dict[str, list[tuple[str, str]]] = {}
    paths = [Path(p) for p in run.report.get("deliverables", [])]
    assert paths, "the report step produced no deliverables"
    for p in paths:
        data = p.read_bytes()
        parts: list[tuple[str, str]]
        if p.suffix == ".pdf":
            pages = PdfReader(io.BytesIO(data)).pages
            parts = [(f"page-{i}", _mask(pg.extract_text(), run)) for i, pg in enumerate(pages)]
        else:
            parts = [("", _mask(data.decode("utf-8", "replace"), run))]
        out[_role(p.name, run)] = parts
    return out


def test_discourse_deliverables_are_identical_across_runs(
    discourse_run: PipelineResult, tmp_path: Path
) -> None:
    second = run_pipeline("discourse", tmp_path / "artifacts", job_id="e2e-dis2")
    a, b = _views(discourse_run), _views(second)
    assert a.keys() == b.keys()

    differing = []
    for role in sorted(a):
        left, right = dict(a[role]), dict(b[role])
        if left.keys() != right.keys():
            differing.append(f"{role}: parts differ {sorted(left.keys() ^ right.keys())[:5]}")
            continue
        for part in left:
            if left[part] != right[part]:
                diff = list(
                    difflib.unified_diff(
                        left[part].splitlines(), right[part].splitlines(), lineterm="", n=0
                    )
                )
                differing.append(
                    f"{role}{'/' + part if part else ''}: {[x[:200] for x in diff[:8]]}"
                )
    assert not differing, f"deliverables differ between identical runs: {differing}"
