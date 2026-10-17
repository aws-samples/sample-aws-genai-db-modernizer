"""build_docs_sample.py copies deliverables into the built help site (#364).

run_pipeline is monkeypatched throughout -- it shells out to this repo's own
scripts/run_*.py as subprocesses, which is appropriate for the e2e suite but
too slow and heavy for a unit test. These tests only check what build_sample
does with the paths it gets back: it must never touch the themed
`sample/index.html` MkDocs already rendered from docs/sample.md, it must
fail fast if that file is missing, and it must copy each recognized
file of the run's deliverables folder under the same name.

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

import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from scripts import build_docs_sample
from src.report.deliverables import RETIRED_FOLDER_FILENAMES


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

    build_docs_sample.build_sample(site_dir, sample="wordpress", job_id="docs-sample")

    assert index_path.read_text(encoding="utf-8") == before


def test_build_sample_copies_deliverables_under_stable_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, site_dir: Path
) -> None:
    _patch_run_pipeline(monkeypatch, tmp_path)

    result = build_docs_sample.build_sample(site_dir, sample="wordpress", job_id="docs-sample")

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

    result = build_docs_sample.build_sample(site_dir, sample="wordpress", job_id="docs-sample")

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

    result = build_docs_sample.build_sample(site_dir, sample="wordpress", job_id="docs-sample")

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
        build_docs_sample.build_sample(site, sample="wordpress", job_id="docs-sample")
