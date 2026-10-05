#!/usr/bin/env python3
"""Render the WordPress sample's deliverables into the built docs site.

Runs the deterministic pipeline (``--llm-mode none``, no model, no AWS
credentials -- same as ``tests/e2e/pipeline.py``) against
``docs/examples/wordpress``, then copies the rendered deliverables into
``<site-dir>/sample/`` with a generated index page, replacing the
placeholder page ``docs/sample.md`` builds as part of ``mkdocs build``.

Deliverables are copied under stable names (``decision-report.html``, not the
per-job, date-stamped filename ``renderers.provenance`` gives each run), so a
link to the sample report survives a rebuild. The engineering report, the one
deliverable rendered as Markdown rather than HTML, is also rendered to
``engineering-report.html`` for in-browser reading; the ``.md`` source stays
available too, for anyone who wants to copy it elsewhere.

Usage:
    uv run python scripts/build_docs_sample.py --site-dir site

Must run after ``mkdocs build`` (or ``mkdocs build --strict``), since it
writes directly into the site output directory rather than through mkdocs.
"""

from __future__ import annotations

import argparse
import html
import json
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from tests.e2e.pipeline import run_pipeline  # noqa: E402

# (filename substring to match against each rendered path, stable name to copy it
# to, human label). "decision-report"/"engineering-report"/"analysis-report" come
# from the per-job filename stem (renderers.provenance), which changes every run;
# the executive summary has a fixed name (pdf_report.FILENAME) shared across every
# job. The editable .pptx deck (pptx_report.FILENAME, stage-only, not published per
# render_deliverables) is deliberately not in this list -- it is not copied.
_DELIVERABLES: list[tuple[str, str, str]] = [
    ("decision-report", "decision-report.html", "Decision Report (HTML)"),
    ("engineering-report", "engineering-report.md", "Engineering Report (Markdown source)"),
    ("analysis-report", "analysis-report.html", "Interactive Analysis Report (HTML)"),
    ("summary-executive-report.pdf", "executive-summary.pdf", "Executive Summary (PDF)"),
]

# Display order on the generated index page (engineering-report.html is added
# dynamically -- see _render_engineering_html -- so it isn't in _DELIVERABLES).
_INDEX_ORDER = [
    "decision-report.html",
    "engineering-report.html",
    "engineering-report.md",
    "analysis-report.html",
    "executive-summary.pdf",
]


def _stable_target(filename: str) -> tuple[str, str] | None:
    for needle, stable_name, label in _DELIVERABLES:
        if needle in filename:
            return stable_name, label
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


def _write_index(dest: Path, labels: dict[str, str]) -> None:
    items = []
    for name in _INDEX_ORDER:
        if name not in labels:
            continue
        items.append(f'<li><a href="{html.escape(name)}">{html.escape(labels[name])}</a></li>')
    body = "\n      ".join(items) if items else "<li>No deliverables were rendered.</li>"
    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Sample report &mdash; Database Modernizer Assessment</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
          max-width: 48rem; margin: 2rem auto; padding: 0 1rem; color: #212529; }}
  h1 {{ font-size: 1.75rem; }}
  .note {{ background: #e7f0fe; border-left: 4px solid #3f51b5; padding: 0.75rem 1rem; margin: 1rem 0; }}
  ul {{ line-height: 1.9; }}
  a {{ color: #3f51b5; }}
  footer {{ margin-top: 2rem; font-size: 0.85rem; color: #6c757d; }}
</style>
</head>
<body>
<p><a href="../">&larr; Back to the help site</a></p>
<h1>Sample report: WordPress</h1>
<p class="note">
  Generated at site build time by <code>scripts/build_docs_sample.py</code>, running the
  deterministic pipeline (<code>--llm-mode none</code>) against the WordPress + WooCommerce
  sample in <code>docs/examples/wordpress</code>. No model call, no AWS credentials.
</p>
<ul>
      {body}
</ul>
<footer>Database Modernizer Assessment is a sample project for educational and evaluation purposes.</footer>
</body>
</html>
"""
    (dest / "index.html").write_text(page, encoding="utf-8")


def build_sample(site_dir: Path, sample: str = "wordpress", job_id: str = "docs-sample") -> dict:
    with tempfile.TemporaryDirectory(prefix="modernizer-docs-sample-") as tmp:
        artifact_root = Path(tmp) / "artifacts"
        result = run_pipeline(sample, artifact_root, job_id=job_id)
        paths = [Path(p) for p in result.report.get("paths", [])]

        dest = site_dir / "sample"
        dest.mkdir(parents=True, exist_ok=True)

        labels: dict[str, str] = {}
        copied: list[Path] = []
        engineering_md: Path | None = None
        for p in paths:
            if not p.exists():
                continue
            match = _stable_target(p.name)
            if match is None:
                continue  # e.g. the editable .pptx deck -- not published
            stable_name, label = match
            target = dest / stable_name
            shutil.copyfile(p, target)
            copied.append(target)
            labels[stable_name] = label
            if stable_name == "engineering-report.md":
                engineering_md = target

        if engineering_md is not None:
            rendered = _render_engineering_html(engineering_md, dest)
            if rendered is not None:
                copied.append(rendered)
                labels["engineering-report.html"] = "Engineering Report (HTML)"

        _write_index(dest, labels)
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
