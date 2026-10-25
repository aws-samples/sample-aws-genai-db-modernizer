#!/usr/bin/env python3
"""Render the WordPress sample's deliverables into the built docs site.

By default it renders the recorded model run committed under
``docs/examples/wordpress/sample-run/`` (a real interactive ``/modernize`` run in
Claude Code; its README names the release): the run's synthesis ``report.json`` and
the collected names the executive report keeps out of its text, copied into a
temporary artifact store and rendered by ``scripts/run_report.py`` with this
checkout's renderers. So a wording or layout fix on main shows up on the page, while
the assessment itself (the routing, the schemas, the model-written summary) stays
the recorded one. A report written by an older release goes through the same facts
backfill every saved report does (``src.report.facts.report_facts``, ADR-032
Decision 4). The sample is refreshed once per release with ``--refresh-from``
(docs/RELEASE_MANAGEMENT.md).

``--deterministic`` runs the deterministic pipeline instead (``--llm-mode none``, no
model, no AWS credentials -- same as ``tests/e2e/pipeline.py``) against the bundled
sample collection; it is the only path for ``--sample discourse``, which has no
recorded run.

Either way it copies the rendered deliverables into ``<site-dir>/sample/`` under
stable names. ``docs/sample.md`` is a normal, themed MkDocs page (in the nav, with
the site's header/search/theme) that links to those stable names with raw HTML
anchors rather than Markdown links, specifically so ``mkdocs build --strict`` --
which runs before this script and therefore before these files exist -- does not
flag them as broken links (MkDocs's strict link check only inspects Markdown-syntax
links, not inline HTML `<a href>` tags). This script never writes or touches
``<site-dir>/sample/index.html`` (#364); it only adds files alongside the one MkDocs
already rendered there.

The files are the run's ``artifacts/<db>/<job>/deliverables/`` folder, copied
as is: the same stable names every run gets (``executive-report.pdf``,
``engineering-report.md``, ... -- ``src.report.deliverables.FOLDER_ENTRIES``), so a
link to the sample report survives a rebuild and the site lists exactly what a
real run hands over. The engineering report, the one deliverable rendered as
Markdown rather than HTML, is also rendered to ``engineering-report.html`` for
in-browser reading; the ``.md`` source stays available too, for anyone who wants
to copy it elsewhere.

No preview image is generated here: the ``docs`` extra only installs mkdocs
and mkdocs-material, neither of which can rasterize the executive report
PDF's first page, and adding a PDF-rasterizing dependency (e.g. PyMuPDF) just
for one static image was judged not worth it. If that trade-off changes,
render the preview here, deterministically, from the PDF this script already
copies -- never commit a hand-made screenshot that can drift from the sample.

Usage:
    uv run python scripts/build_docs_sample.py --site-dir site
    uv run python scripts/build_docs_sample.py --site-dir site --deterministic
    uv run python scripts/build_docs_sample.py --refresh-from artifacts/wordpress/<job>

Must run after ``mkdocs build`` (or ``mkdocs build --strict``): it needs
``<site-dir>/sample/index.html`` to already exist (MkDocs renders it from
``docs/sample.md``) and fails fast if it doesn't. ``--refresh-from`` needs no site.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts.run_report import run as run_report  # noqa: E402
from src.report.deliverables import FOLDER_FILENAMES  # noqa: E402
from tests.e2e.pipeline import run_pipeline  # noqa: E402

# The recorded model run each sample's page renders (its README names the release).
SAMPLE_RUN_DIR = REPO / "docs" / "examples" / "wordpress" / "sample-run"
SAMPLE_RUNS: dict[str, Path] = {"wordpress": SAMPLE_RUN_DIR}
SAMPLE_RUN_README = "README.md"
COLLECTOR_KEY = Path("collector") / "output.json"
_SYNTHESIS_VERSION = re.compile(r"^v(\d+)$")

# What a committed sample must never carry, by kind. RELEASE_MANAGEMENT.md lists
# the same kinds (tests/unit/scripts/test_build_docs_sample.py keeps them in step).
# A query id is 64 hex characters, a float has a dot and a UUID has hyphens, so a
# 12-digit run inside any of them is not an account id. A regex list cannot judge
# free text (a customer's name, an internal reference in model prose): a person
# reads the refreshed report's diff before it is merged.
SCAN_BASELINE = REPO / ".secrets.baseline"
_PUBLIC_URL_HOSTS = (
    r"github\.com/aws-samples/",
    r"aws-samples\.github\.io/",
    r"docs\.aws\.amazon\.com/",
    r"aws\.amazon\.com/",
)
_PRIVATE_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "local path",
        re.compile(
            r"(?<![\w.])/(?:Users|home|root|opt|var|private|tmp|mnt|Volumes)/"
            r"|[A-Za-z]:\\{1,2}(?:Users|Documents and Settings)"
            r"|~/"
        ),
    ),
    ("e-mail address", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9.-]+")),
    (
        "non-public URL",
        re.compile(r"\b(?:https?|ftp|s3|file)://(?!(?:" + "|".join(_PUBLIC_URL_HOSTS) + r"))\S+"),
    ),
    (
        "internal hostname",
        re.compile(
            r"\b[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.(?:internal|corp|local|lan|intranet|localdomain)\b"
            r"|\blocalhost\b",
            re.IGNORECASE,
        ),
    ),
    (
        "IP address",
        re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])|\bip-\d{1,3}(?:-\d{1,3}){3}\b"),
    ),
    ("AWS account id", re.compile(r"(?<![\w.-])\d{12}(?![\w.-])")),
    ("AWS ARN", re.compile(r"\barn:aws")),
    ("AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    (
        "API token",
        re.compile(
            r"\bsk-(?:ant-)?[A-Za-z0-9_-]{20,}"
            r"|\b(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{30,}|\bgithub_pat_[A-Za-z0-9_]{30,}"
            r"|\bglpat-[A-Za-z0-9_-]{20,}|\bxox[abposr]-[A-Za-z0-9-]{10,}"
            r"|\bAIza[0-9A-Za-z_-]{35}|\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}"
            r"|\bBearer\s+[A-Za-z0-9._~+/-]{20,}"
            r"|aws_secret_access_key|aws_session_token",
            re.IGNORECASE,
        ),
    ),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)
PRIVATE_DETAIL_KINDS: tuple[str, ...] = (
    *(kind for kind, _ in _PRIVATE_MARKERS),
    "user name",
)


def local_user_names() -> tuple[str, ...]:
    """This machine's user names (the OS login and the home folder's name), which a
    refreshed sample must not carry. Names shorter than 3 characters are skipped."""
    import getpass

    names: set[str] = set()
    for get in (getpass.getuser, lambda: Path.home().name):
        try:
            name = get()
        except Exception:  # noqa: BLE001 - no user name is no user name to look for
            name = ""
        if len(name) >= 3:
            names.add(name)
    return tuple(sorted(names))


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
  /* A model run's tables carry long unbroken names (schema-qualified tables, target
     objects, query ids): let them wrap rather than widen the page (#1260). */
  th, td, code {{ overflow-wrap: anywhere; }}
  code, pre {{ background: #f1f3f5; }}
  pre {{ overflow-x: auto; }}
  a {{ color: #3f51b5; }}
</style>
</head>
<body>
<header><nav aria-label="Sample report"><a href="./">&larr; Back to the sample report</a> &middot; <a href="engineering-report.md">Markdown source</a></nav></header>
<main>
<article>
{body}
</article>
</main>
</body>
</html>
"""
    out = dest / "engineering-report.html"
    out.write_text(page, encoding="utf-8")
    return out


def private_markers(text: str, user_names: tuple[str, ...] = ()) -> list[str]:
    """Each kind of private detail ``text`` carries (empty when it is clean).

    ``user_names`` (``local_user_names()`` at refresh time) are matched as whole
    words, case-insensitively. The committed-file test passes none: a CI runner's
    login (``runner``) is an ordinary word.
    """
    found = [kind for kind, pattern in _PRIVATE_MARKERS if pattern.search(text)]
    if any(
        re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", text, re.IGNORECASE) for n in user_names
    ):
        found.append("user name")
    return found


def _relative_to(path: str, root: Path) -> str:
    p = Path(path)
    return p.resolve().relative_to(root.resolve()).as_posix() if p.is_absolute() else p.as_posix()


def update_scan_baseline(
    run_dir: Path, baseline_file: Path = SCAN_BASELINE, root: Path = REPO
) -> dict[str, int]:
    """Re-scan ``run_dir``'s files with detect-secrets and replace their entries in
    ``baseline_file``, so the commit hook accepts the refreshed sample's query ids
    (high-entropy hex, not secrets) and still flags anything new in those files.

    Every other entry, and the baseline's settings and timestamp, stay as stored.
    Returns the number of entries per file.
    """
    from detect_secrets import SecretsCollection
    from detect_secrets.core import baseline as ds_baseline

    data = json.loads(baseline_file.read_text(encoding="utf-8"))
    ds_baseline.load(data, str(baseline_file))  # the baseline's plugins and filters
    prefix = run_dir.resolve().relative_to(root.resolve()).as_posix() + "/"
    files = sorted(
        p.resolve().relative_to(root.resolve()).as_posix()
        for p in run_dir.rglob("*")
        if p.is_file()
    )
    scanned = SecretsCollection(root=str(root))
    for rel in files:
        scanned.scan_file(rel)
    # The collection names each entry by its scanned (absolute) path; the baseline
    # names files relative to the repository, and must carry no local path.
    found = {_relative_to(name, root): entries for name, entries in scanned.json().items()}
    for rel, entries in found.items():
        for entry in entries:
            entry["filename"] = rel
    results = {k: v for k, v in data["results"].items() if not k.startswith(prefix)}
    results.update({k: v for k, v in found.items() if v})
    data["results"] = dict(sorted(results.items()))
    baseline_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return {rel: len(found.get(rel) or []) for rel in files}


def sample_run_report(run_dir: Path) -> Path:
    """The committed run's synthesis report: its highest ``synthesis/v<N>/report.json``."""
    found = sorted(
        (int(m.group(1)), d / "report.json")
        for d in (run_dir / "synthesis").glob("v*")
        if (m := _SYNTHESIS_VERSION.match(d.name)) and (d / "report.json").is_file()
    )
    if not found:
        raise FileNotFoundError(f"no synthesis/v<N>/report.json under {run_dir}")
    return found[-1][1]


def render_sample_run(run_dir: Path, artifact_root: Path) -> dict[str, Any]:
    """Render ``run_dir``'s recorded run with this checkout's renderers.

    Copies it into ``artifact_root/<db>/<job>/`` (the names its report carries) and
    runs ``scripts/run_report.py``'s ``run``; returns its result. Anything short of a
    complete render (a deliverable that failed) is an error: the sample page shows
    every deliverable or the build fails.
    """
    from src.contracts.synthesis_output import SynthesisOutputContract
    from src.shared.contract_versioning import check_contract_version
    from src.shared.database_name import validate_database_name
    from src.shared.job_id import validate_job_id

    report_path = sample_run_report(run_dir)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    # A report of another MAJOR (with no adapter) is no sample this code can render.
    check_contract_version(report, SynthesisOutputContract, artifact=str(report_path))
    db = validate_database_name(report.get("database_name"))
    job_id = validate_job_id(report.get("job_id"))
    job_dir = artifact_root / db / job_id
    shutil.copytree(run_dir, job_dir, ignore=shutil.ignore_patterns(SAMPLE_RUN_README))
    result = run_report(job_id, db, str(artifact_root), None)
    if result.get("status") != "complete":
        raise RuntimeError(
            f"the sample run did not render completely: {result.get('message') or result.get('errors')}"
        )
    return {**result, "db": db, "job_id": job_id}


def _collected_names(collector_output: dict[str, Any]) -> dict[str, Any]:
    """The part of a collector output the renderers read: every collected name
    (``src.report.executive_report.collected_identifiers``), nothing else -- no
    query text, no metrics, no host metadata."""
    schema = collector_output.get("database_schema") or {}
    kinds = (("tables", "table"), ("views", "view"), ("procedures", "procedure"))
    names: dict[str, Any] = {}
    for key, kind in (*kinds, ("triggers", "trigger")):
        rows = []
        for obj in schema.get(key) or []:
            if not isinstance(obj, dict):
                continue
            row = {
                k: obj[k]
                for k in (f"{kind}_id", f"{kind}_name", "schema_name")
                if obj.get(k) is not None
            }
            columns = [
                {"column_name": c["column_name"]}
                for c in obj.get("columns") or []
                if isinstance(c, dict) and c.get("column_name")
            ]
            if columns:
                row["columns"] = columns
            rows.append(row)
        names[key] = rows
    return {
        "_note": (
            "Trimmed by scripts/build_docs_sample.py --refresh-from: only the collected "
            "names the executive report keeps out of its text."
        ),
        "database_schema": names,
    }


def _routing_records(job_dir: Path, report: dict[str, Any], report_src: Path) -> dict[Path, str]:
    """The saved routing records the detailed engineering report reads (#1472), as
    ``{path relative to the job folder: text}``: the effective assignment's lineage
    (``assignment/v<N>/assignment.json``, following ``previous_version`` back, so an
    abandoned branch stays out), the effective version's ``access_patterns.json`` and
    each engine's ``analysis-<engine>/analysis.json`` (not its decision trace). A record
    the job did not save is left out; the report states the absence. Reads the same
    records ``src.shared.routing_detail.load_routing_detail`` does."""
    from src.shared.routing_detail import ACCESS_PATTERNS_FILE, effective_version_of

    key = "job/" + report_src.relative_to(job_dir).as_posix()
    version = effective_version_of(report, key)
    records: dict[Path, str] = {}

    def keep(rel: Path) -> dict[str, Any] | None:
        path = job_dir / rel
        if not path.is_file():
            return None
        text = path.read_text(encoding="utf-8").rstrip("\n") + "\n"
        records[rel] = text
        data = json.loads(text)
        return data if isinstance(data, dict) else None

    seen: set[int] = set()
    current: int | None = version
    while isinstance(current, int) and current >= 1 and current not in seen:
        seen.add(current)
        assignment = keep(Path("assignment") / f"v{current}" / "assignment.json")
        if assignment is None:
            break
        current = assignment.get("previous_version")
    if version:
        keep(Path("assignment") / f"v{version}" / ACCESS_PATTERNS_FILE)
    for path in sorted(job_dir.glob("analysis-*/analysis.json")):
        keep(path.relative_to(job_dir))
    return records


def refresh_sample_run(
    job_dir: Path,
    run_dir: Path = SAMPLE_RUN_DIR,
    baseline_file: Path = SCAN_BASELINE,
    root: Path = REPO,
) -> dict[str, Any]:
    """Replace ``run_dir``'s recorded run with the one in ``job_dir``.

    Copies the job's latest synthesis ``report.json``, the collected names
    (``_collected_names``) and the routing records the detailed engineering report
    reads (``_routing_records``, #1472) -- all the renderers read -- and keeps ``run_dir``'s README,
    which names the release (update it by hand). Refuses the job when either of the
    two outputs carries a private detail (``private_markers``, with this machine's
    user names), then regenerates the folder's detect-secrets baseline entries
    (``update_scan_baseline``). A person still reads the refreshed report's diff
    before it is merged: the check cannot judge free text.
    """
    from src.contracts.collector_output import CollectorOutputContract
    from src.contracts.synthesis_output import SynthesisOutputContract
    from src.shared.contract_versioning import check_contract_version

    report_src = sample_run_report(job_dir)
    # The run's report as written, with the final newline the repo's hooks require.
    report_text = report_src.read_text(encoding="utf-8").rstrip("\n") + "\n"
    check_contract_version(
        json.loads(report_text), SynthesisOutputContract, artifact=str(report_src)
    )
    collector = json.loads((job_dir / COLLECTOR_KEY).read_text(encoding="utf-8"))
    check_contract_version(
        collector, CollectorOutputContract, artifact=str(job_dir / COLLECTOR_KEY)
    )
    collector_text = json.dumps(_collected_names(collector), indent=2) + "\n"
    routing = _routing_records(job_dir, json.loads(report_text), report_src)
    texts = [("report.json", report_text), ("collector names", collector_text)]
    texts += [(rel.as_posix(), text) for rel, text in routing.items()]
    for name, text in texts:
        found = private_markers(text, local_user_names())
        if found:
            raise ValueError(f"{name} carries a private detail ({', '.join(found)}); not copied")

    for old in (
        run_dir / "synthesis",
        run_dir / "collector",
        run_dir / "assignment",
        *run_dir.glob("analysis-*"),
    ):
        if old.exists():
            shutil.rmtree(old)
    report_dest = run_dir / report_src.relative_to(job_dir)
    report_dest.parent.mkdir(parents=True, exist_ok=True)
    report_dest.write_text(report_text, encoding="utf-8")
    collector_dest = run_dir / COLLECTOR_KEY
    collector_dest.parent.mkdir(parents=True, exist_ok=True)
    collector_dest.write_text(collector_text, encoding="utf-8")
    routing_dests = []
    for rel, text in routing.items():
        dest = run_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        routing_dests.append(str(dest))
    baseline = update_scan_baseline(run_dir, baseline_file, root)
    return {
        "status": "complete",
        "run_dir": str(run_dir),
        "files": [str(report_dest), str(collector_dest), *routing_dests],
        "scan_baseline_entries": baseline,
    }


def build_sample(
    site_dir: Path,
    sample: str = "wordpress",
    job_id: str = "docs-sample",
    deterministic: bool = False,
    run_dir: Path | None = None,
) -> dict:
    dest = site_dir / "sample"
    if not (dest / "index.html").exists():
        raise FileNotFoundError(
            f"{dest / 'index.html'} does not exist -- run `mkdocs build --strict` first so "
            "MkDocs renders the themed sample page from docs/sample.md"
        )
    if not deterministic:
        run_dir = run_dir or SAMPLE_RUNS.get(sample)
        if run_dir is None:
            raise ValueError(f"no recorded run for the {sample} sample; pass --deterministic")

    with tempfile.TemporaryDirectory(prefix="modernizer-docs-sample-") as tmp:
        artifact_root = Path(tmp) / "artifacts"
        if deterministic:
            result = run_pipeline(sample, artifact_root, job_id=job_id)
            # The run's deliverables folder (run_report.py), already under stable names.
            paths = [Path(p) for p in result.report.get("deliverables", [])]
            source, rendered_job = "deterministic", result.job_id
        else:
            assert run_dir is not None  # nosec B101 - narrowed above
            rendered = render_sample_run(run_dir, artifact_root)
            paths = [Path(p) for p in rendered.get("deliverables", [])]
            source, rendered_job = "sample-run", rendered["job_id"]

        copied: list[Path] = []
        engineering_md: Path | None = None
        for p in paths:
            if not p.exists() or p.name not in FOLDER_FILENAMES:
                continue
            target = dest / p.name
            shutil.copyfile(p, target)
            copied.append(target)
            if p.name == "engineering-report.md":
                engineering_md = target

        if engineering_md is not None:
            html = _render_engineering_html(engineering_md, dest)
            if html is not None:
                copied.append(html)

        return {
            "status": "complete",
            "sample": sample,
            "source": source,
            "job_id": rendered_job,
            "files": [str(p) for p in copied],
        }


def _refresh_main(job_dir: Path) -> int:
    """``--refresh-from``: print the refresh's JSON status line; the exit code."""
    try:
        refreshed = refresh_sample_run(job_dir.resolve())
    except Exception as e:  # noqa: BLE001 - this script must always print JSON
        print(json.dumps({"status": "error", "message": f"{type(e).__name__}: {e}"}))
        return 1
    # Counts and paths only: the baseline's hashed entries are never printed.
    print(
        json.dumps(
            {
                "status": refreshed["status"],
                "run_dir": refreshed["run_dir"],
                "files": refreshed["files"],
                "scan_baseline_entries": refreshed["scan_baseline_entries"],
            }
        )
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site-dir", default="site", help="mkdocs build output directory")
    parser.add_argument("--sample", default="wordpress", choices=["wordpress", "discourse"])
    parser.add_argument("--job-id", default="docs-sample", help="job id of a --deterministic run")
    parser.add_argument(
        "--deterministic",
        action="store_true",
        help="run the deterministic pipeline (no model) instead of rendering the recorded run",
    )
    parser.add_argument(
        "--refresh-from",
        metavar="JOB_DIR",
        help=(
            "replace the committed WordPress sample run with this job folder's "
            "(artifacts/wordpress/<job>); renders nothing"
        ),
    )
    args = parser.parse_args()

    if args.refresh_from:
        sys.exit(_refresh_main(Path(args.refresh_from)))

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
        result = build_sample(
            site_dir, sample=args.sample, job_id=args.job_id, deterministic=args.deterministic
        )
    except Exception as e:  # noqa: BLE001 - this script must always print JSON
        print(json.dumps({"status": "error", "message": f"{type(e).__name__}: {e}"}))
        sys.exit(1)

    print(json.dumps(result))
    sys.exit(0)


if __name__ == "__main__":
    main()
