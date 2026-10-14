#!/usr/bin/env python3
"""Render the WordPress sample's deliverables into the built docs site.

Runs the deterministic pipeline (``--llm-mode none``, no model, no AWS
credentials -- same as ``tests/e2e/pipeline.py``) against
``docs/examples/wordpress``, then copies the rendered deliverables into
``<site-dir>/sample/`` under stable names. ``docs/sample.md`` is a normal,
themed MkDocs page (in the nav, with the site's header/search/theme) that
links to those stable names with raw HTML anchors rather than Markdown
links, specifically so ``mkdocs build --strict`` -- which runs before this
script and therefore before these files exist -- does not flag them as
broken links (MkDocs's strict link check only inspects Markdown-syntax
links, not inline HTML `<a href>` tags). This script never writes or
touches ``<site-dir>/sample/index.html`` (#364); it only adds files
alongside the one MkDocs already rendered there.

Deliverables are copied under stable names (``decision-report.html``, not the
per-job, date-stamped filename ``formatting.provenance`` gives each run), so a
link to the sample report survives a rebuild. The engineering report, the one
deliverable rendered as Markdown rather than HTML, is also rendered to
``engineering-report.html`` for in-browser reading; the ``.md`` source stays
available too, for anyone who wants to copy it elsewhere.

No preview image is generated here: the ``docs`` extra only installs mkdocs
and mkdocs-material, neither of which can rasterize the executive summary
PDF's first page, and adding a PDF-rasterizing dependency (e.g. PyMuPDF) just
for one static image was judged not worth it. If that trade-off changes,
render the preview here, deterministically, from the PDF this script already
copies -- never commit a hand-made screenshot that can drift from the sample.

Usage:
    uv run python scripts/build_docs_sample.py --site-dir site

Must run after ``mkdocs build`` (or ``mkdocs build --strict``): it needs
``<site-dir>/sample/index.html`` to already exist (MkDocs renders it from
``docs/sample.md``) and fails fast if it doesn't.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from tests.e2e.pipeline import run_pipeline  # noqa: E402

# (filename substring to match against each rendered path, stable name to copy it
# to). "decision-report"/"engineering-report"/"analysis-report" come from the
# per-job filename stem (formatting.provenance), which changes every run; the
# executive summary has a fixed name (pdf_report.FILENAME) shared across every
# job. The editable .pptx deck (pptx_report.FILENAME, stage-only, not published
# per render_deliverables) is deliberately not in this list -- it is not
# copied. These stable names are also the ones docs/sample.md links to.
_DELIVERABLES: list[tuple[str, str]] = [
    ("decision-report", "decision-report.html"),
    ("engineering-report", "engineering-report.md"),
    ("analysis-report", "analysis-report.html"),
    ("summary-executive-report.pdf", "executive-summary.pdf"),
]


def _stable_target(filename: str) -> str | None:
    for needle, stable_name in _DELIVERABLES:
        if needle in filename:
            return stable_name
    return None


def _render_engineering_html(md_path: Path, dest: Path) -> Path | None:
    """Render the engineering report's Markdown to a readable HTML page.

    Returns ``None`` (not an error -- the ``.md`` download still works) if the
    ``markdown`` package isn't installed. It ships with ``mkdocs``, which this
    script always runs alongside, but isn't a standalone dependency of this
    script.
    """
    try:
        import markdown
    except ImportError:
        return None

    body = markdown.markdown(
        md_path.read_text(encoding="utf-8"),
        extensions=["tables", "fenced_code", "toc"],
    )
    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Engineering Report &mdash; Database Modernizer Assessment sample</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
          max-width: 56rem; margin: 2rem auto; padding: 0 1rem; color: #212529; line-height: 1.6; }}
  table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; }}
  th, td {{ border: 1px solid #dee2e6; padding: 0.4rem 0.6rem; text-align: left; }}
  code, pre {{ background: #f1f3f5; }}
  a {{ color: #3f51b5; }}
</style>
</head>
<body>
<p><a href="./">&larr; Back to the sample report</a> &middot; <a href="engineering-report.md">Markdown source</a></p>
<article>
{body}
</article>
</body>
</html>
"""
    out = dest / "engineering-report.html"
    out.write_text(page, encoding="utf-8")
    return out


def build_sample(site_dir: Path, sample: str = "wordpress", job_id: str = "docs-sample") -> dict:
    dest = site_dir / "sample"
    if not (dest / "index.html").exists():
        raise FileNotFoundError(
            f"{dest / 'index.html'} does not exist -- run `mkdocs build --strict` first so "
            "MkDocs renders the themed sample page from docs/sample.md"
        )

    with tempfile.TemporaryDirectory(prefix="modernizer-docs-sample-") as tmp:
        artifact_root = Path(tmp) / "artifacts"
        result = run_pipeline(sample, artifact_root, job_id=job_id)
        paths = [Path(p) for p in result.report.get("paths", [])]

        copied: list[Path] = []
        engineering_md: Path | None = None
        for p in paths:
            if not p.exists():
                continue
            stable_name = _stable_target(p.name)
            if stable_name is None:
                continue  # e.g. the editable .pptx deck -- not published
            target = dest / stable_name
            shutil.copyfile(p, target)
            copied.append(target)
            if stable_name == "engineering-report.md":
                engineering_md = target

        if engineering_md is not None:
            rendered = _render_engineering_html(engineering_md, dest)
            if rendered is not None:
                copied.append(rendered)

        return {
            "status": "complete",
            "sample": sample,
            "job_id": result.job_id,
            "files": [str(p) for p in copied],
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site-dir", default="site", help="mkdocs build output directory")
    parser.add_argument("--sample", default="wordpress", choices=["wordpress", "discourse"])
    parser.add_argument("--job-id", default="docs-sample")
    args = parser.parse_args()

    site_dir = Path(args.site_dir).resolve()
    if not site_dir.exists():
        print(
            json.dumps(
                {
                    "status": "error",
                    "message": f"{site_dir} does not exist -- run `mkdocs build` first",
                }
            )
        )
        sys.exit(1)

    try:
        result = build_sample(site_dir, sample=args.sample, job_id=args.job_id)
    except Exception as e:  # noqa: BLE001 - this script must always print JSON
        print(json.dumps({"status": "error", "message": f"{type(e).__name__}: {e}"}))
        sys.exit(1)

    print(json.dumps(result))
    sys.exit(0)


if __name__ == "__main__":
    main()
