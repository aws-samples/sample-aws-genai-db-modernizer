#!/usr/bin/env python3
"""Rubric-based quality judge for one job's rendered deliverables.

Usage:
    uv run python ci/llm/judge.py --artifact-root R --db D --job J --out judge.json

Scores the deliverables staged next to a synthesis report (``report.json``, the
decision report HTML, the engineering report Markdown, and -- when present and
``pypdf`` is installed -- the executive summary PDF) against the six criteria
in ``ci/llm/rubric.md`` (``grounded``, ``justified_engines``, ``cost``,
``risks``, ``roadmap``, ``tone``), by asking the same CLI and auth as the
headless `/modernize` run to grade them.

Model invocation
-----------------
Calls ``$CLAUDE_BIN`` (default ``claude``) exactly like the headless run:
``claude -p "<prompt>" --output-format json --tools "" --permission-mode
dontAsk --settings <settings>``.

Two flags named in the original task plan turned out not to exist on the
installed CLI (v2.1.288 ASBX Claude Code) per ``claude --help``:

* ``--max-turns`` is not a documented flag at all on this version -- omitted.
* ``--disallowedTools`` only *removes individual named tools* from the
  default set; it cannot disable every tool. ``--tools`` can: per its help
  text, ``--tools ""`` "disable[s] all tools". Since a tool-disabling flag
  does exist, this uses ``--tools ""`` instead of relying on turn limits (no
  tool use is needed for grading text anyway, so this is pure defense in
  depth against a prompt-injected tool call in the judged artifacts).

``--output-format json`` is documented by ``--help`` only as "json (single
result)" -- the field names it emits (``result``, ``is_error``,
``total_cost_usd``, ``model``, ...) are not spelled out in the CLI's own
help text, so parsing here is deliberately tolerant: every field read off
the outer CLI JSON uses ``.get()`` with a safe default, and anything
unexpected about the outer JSON surfaces as a judge error (exit 2) rather
than a crash. The CLI's own JSON only wraps the *model's* answer (its
``result`` field, a string) -- that string is parsed a second time as the
rubric JSON object, tolerating a fenced ```json code block.

Exit codes: 0 pass, 1 fail (parsed fine, didn't meet the rubric's pass rule),
2 judge error (bad/missing CLI output, missing deliverable, bad rubric file).
The output JSON is written in all three cases; on error it is
``{"error": "..."}``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess  # nosec B404 -- intentional subprocess use to invoke the claude CLI
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SETTINGS_PATH = REPO_ROOT / ".claude" / "settings.ci.json"
DEFAULT_RUBRIC_PATH = Path(__file__).resolve().parent / "rubric.md"

# Six rubric criteria, in the order they're scored. Keys match the JSON object
# asked of the model and the front matter/body of ci/llm/rubric.md.
CRITERIA: tuple[str, ...] = (
    "grounded",
    "justified_engines",
    "cost",
    "risks",
    "roadmap",
    "tone",
)

# Character budgets for each input truncated into the prompt. Documented here,
# not derived, so changing one is a one-line diff with an obvious blast radius.
REPORT_JSON_CHAR_BUDGET = 8_000
DECISION_HTML_CHAR_BUDGET = 6_000
ENGINEERING_MD_CHAR_BUDGET = 6_000
PDF_TEXT_CHAR_BUDGET = 4_000

_FRONT_MATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)
_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


class JudgeError(Exception):
    """Anything that makes grading impossible: missing deliverable, bad rubric
    file, bad CLI invocation, or a judge response that doesn't parse/validate.
    Always surfaces as exit code 2."""


# ---------------------------------------------------------------------------
# Rubric
# ---------------------------------------------------------------------------


def load_rubric(path: Path) -> tuple[float, int, str]:
    """Return ``(pass_mean, min_score, body)`` from a rubric file's YAML front
    matter and the Markdown body that follows it (the body is embedded verbatim
    in the judge prompt)."""
    try:
        text = path.read_text()
    except OSError as exc:
        raise JudgeError(f"could not read rubric file {path}: {exc}") from exc

    match = _FRONT_MATTER_RE.match(text)
    if not match:
        raise JudgeError(f"rubric file {path} has no '---' front matter")

    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise JudgeError(f"rubric file {path} has invalid front matter YAML: {exc}") from exc

    if "pass_mean" not in meta or "min_score" not in meta:
        raise JudgeError(f"rubric file {path} front matter missing pass_mean/min_score: {meta!r}")

    try:
        pass_mean = float(meta["pass_mean"])
        min_score = int(meta["min_score"])
    except (TypeError, ValueError) as exc:
        raise JudgeError(f"rubric file {path} has non-numeric thresholds: {meta!r}") from exc

    return pass_mean, min_score, match.group(2)


# ---------------------------------------------------------------------------
# Deliverable location (synthesis/v*/, highest numeric version)
# ---------------------------------------------------------------------------


def find_synthesis_dir(artifact_root: Path, db: str, job: str) -> Path:
    """Return the synthesis directory for ``db``/``job``, preferring the
    highest-numbered ``synthesis/v{N}/`` present, falling back to the legacy
    unversioned ``referee-synthesis/`` (mirrors
    ``src.report.analysis_report.synthesis_report_key``'s version preference,
    without needing a ``LocalArtifactStore`` for a plain filesystem walk)."""
    job_dir = artifact_root / db / job
    synthesis_root = job_dir / "synthesis"
    versions: list[tuple[int, Path]] = []
    if synthesis_root.is_dir():
        for child in synthesis_root.iterdir():
            m = re.match(r"^v(\d+)$", child.name)
            if m and child.is_dir():
                versions.append((int(m.group(1)), child))
    if versions:
        return max(versions, key=lambda item: item[0])[1]

    legacy = job_dir / "referee-synthesis"
    if legacy.is_dir():
        return legacy

    raise JudgeError(f"no synthesis directory found under {job_dir}")


def locate_deliverables(artifact_root: Path, db: str, job: str) -> dict[str, Path | None]:
    """Return the paths of the deliverables to grade.

    ``report.json``, the decision report, and the engineering report are
    rendered unconditionally by ``render_deliverables`` whenever synthesis
    succeeded, so a missing one here is a clear setup error (exit 2). The PDF
    is optional there too (an executive-summary rendering failure still lets
    the rest of the deliverables stage) and is treated the same way here:
    ``None`` when absent, recorded as a note rather than an error.
    """
    synthesis_dir = find_synthesis_dir(artifact_root, db, job)

    report_path = synthesis_dir / "report.json"
    if not report_path.is_file():
        raise JudgeError(f"missing report.json in {synthesis_dir}")

    decision_matches = sorted(synthesis_dir.glob("*decision-report*.html"))
    if not decision_matches:
        raise JudgeError(f"missing decision report (*decision-report*.html) in {synthesis_dir}")

    engineering_matches = sorted(synthesis_dir.glob("*engineering-report*.md"))
    if not engineering_matches:
        raise JudgeError(f"missing engineering report (*engineering-report*.md) in {synthesis_dir}")

    pdf_matches = sorted(synthesis_dir.glob("*.pdf"))

    return {
        "report_json": report_path,
        "decision_html": decision_matches[0],
        "engineering_md": engineering_matches[0],
        "pdf": pdf_matches[0] if pdf_matches else None,
    }


# ---------------------------------------------------------------------------
# Input extraction
# ---------------------------------------------------------------------------


class _TextExtractor(HTMLParser):
    """Minimal HTML-to-text: drops tags and <script>/<style> contents, keeps
    everything else. ``HTMLParser`` converts character references to text by
    default, so entities need no separate handling."""

    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            text = data.strip()
            if text:
                self._chunks.append(text)

    def text(self) -> str:
        return "\n".join(self._chunks)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    return parser.text()


def read_pdf_text(path: Path | None) -> tuple[str, str | None]:
    """Return ``(text, note)``. ``note`` is set (and ``text`` empty) whenever
    the PDF section is skipped: no PDF deliverable, ``pypdf`` not installed
    (it ships in the ``e2e`` extra, imported lazily so the judge still runs
    where only the base/dev extras are installed), or extraction itself
    failed."""
    if path is None:
        return "", "PDF deliverable not found; skipped."

    try:
        import pypdf
    except ImportError:
        return "", "pypdf not installed (requires the 'e2e' extra); PDF section skipped."

    try:
        reader = pypdf.PdfReader(str(path))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as exc:  # noqa: BLE001 - PDF extraction is best-effort
        return "", f"failed to extract PDF text ({type(exc).__name__}: {exc}); skipped."
    return text, None


def truncate(text: str, budget: int) -> str:
    if len(text) <= budget:
        return text
    return text[:budget] + f"\n...[truncated to {budget} characters]"


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------


def build_prompt(rubric_body: str, db: str, job: str, deliverables: dict[str, Path | None]) -> str:
    report_json_text = truncate(
        deliverables["report_json"].read_text(), REPORT_JSON_CHAR_BUDGET  # type: ignore[union-attr]
    )
    decision_text = truncate(
        html_to_text(deliverables["decision_html"].read_text()),  # type: ignore[union-attr]
        DECISION_HTML_CHAR_BUDGET,
    )
    engineering_text = truncate(
        deliverables["engineering_md"].read_text(), ENGINEERING_MD_CHAR_BUDGET  # type: ignore[union-attr]
    )
    pdf_text, pdf_note = read_pdf_text(deliverables.get("pdf"))
    pdf_text = truncate(pdf_text, PDF_TEXT_CHAR_BUDGET)

    criteria_list = ", ".join(CRITERIA)
    sections = [
        f"You are grading the generated deliverables for database modernization "
        f'job "{job}" (database "{db}") against the rubric below. Score each of '
        f"the six criteria -- {criteria_list} -- from 1 to 5 using the rubric's "
        "anchors for 1, 3 and 5.",
        "## Rubric\n" + rubric_body.strip(),
        "## report.json (ground truth)\n" + report_json_text,
        "## Decision report (text extracted from HTML)\n" + decision_text,
        "## Engineering report (Markdown)\n" + engineering_text,
    ]
    if pdf_note:
        sections.append(f"## Executive summary PDF\n(skipped: {pdf_note})")
    else:
        sections.append("## Executive summary PDF (text)\n" + pdf_text)

    sections.append(
        "Respond with ONLY a JSON object of this exact shape, integer scores 1-5, "
        "no prose outside the JSON object:\n"
        '{"scores": {"grounded": n, "justified_engines": n, "cost": n, "risks": n, '
        '"roadmap": n, "tone": n}, '
        '"notes": {"grounded": "...", "justified_engines": "...", "cost": "...", '
        '"risks": "...", "roadmap": "...", "tone": "..."}}'
    )
    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Model invocation + response parsing
# ---------------------------------------------------------------------------


def call_claude_cli(prompt: str, settings_path: Path, claude_bin: str) -> dict[str, Any]:
    cmd = [
        claude_bin,
        "-p",
        prompt,
        "--output-format",
        "json",
        "--tools",
        "",
        "--permission-mode",
        "dontAsk",
        "--settings",
        str(settings_path),
    ]
    try:
        proc = subprocess.run(  # nosec B603 # nosemgrep: dangerous-subprocess-use-audit, dangerous-subprocess-use
            cmd, capture_output=True, text=True, timeout=300
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise JudgeError(f"failed to invoke claude CLI ({claude_bin}): {exc}") from exc

    if proc.returncode != 0:
        raise JudgeError(
            f"claude CLI ({claude_bin}) exited {proc.returncode}: {proc.stderr[:2000]}"
        )

    try:
        parsed: dict[str, Any] = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise JudgeError(
            f"claude CLI did not return valid JSON on stdout: {exc}; "
            f"stdout={proc.stdout[:2000]!r}"
        ) from exc
    return parsed


def extract_json_object(text: str) -> dict[str, Any]:
    """Parse ``text`` as JSON, tolerating a ```json fenced code block (and, as
    a last resort, surrounding prose) around the object."""
    fence_match = _FENCE_RE.search(text)
    candidate = fence_match.group(1) if fence_match else text
    parsed: dict[str, Any]
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        parsed = json.loads(candidate[start : end + 1])
    return parsed


def validate_scores(payload: dict[str, Any]) -> tuple[dict[str, int], dict[str, str]]:
    if "scores" not in payload or not isinstance(payload["scores"], dict):
        raise JudgeError(f"judge response missing a 'scores' object: {payload!r}")
    scores_raw = payload["scores"]
    notes_candidate = payload.get("notes")
    notes_raw: dict[str, Any] = notes_candidate if isinstance(notes_candidate, dict) else {}

    scores: dict[str, int] = {}
    for key in CRITERIA:
        if key not in scores_raw:
            raise JudgeError(f"judge response missing score for criterion '{key}': {payload!r}")
        value = scores_raw[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise JudgeError(f"criterion '{key}' score {value!r} is not a number")
        if value != int(value) or not (1 <= value <= 5):
            raise JudgeError(f"criterion '{key}' score {value!r} is not an integer in 1-5")
        scores[key] = int(value)

    notes = {key: str(notes_raw.get(key, "")) for key in CRITERIA}
    return scores, notes


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_judge(
    *,
    artifact_root: str,
    db: str,
    job: str,
    claude_bin: str | None = None,
    settings_path: Path | None = None,
    rubric_path: Path | None = None,
) -> tuple[dict[str, Any], int]:
    """Run the full judge pipeline. Returns ``(result, exit_code)``; never
    raises -- every failure mode is caught and turned into
    ``({"error": "..."}, 2)``."""
    claude_bin = claude_bin or os.environ.get("CLAUDE_BIN", "claude")
    settings_path = settings_path or DEFAULT_SETTINGS_PATH
    rubric_path = rubric_path or DEFAULT_RUBRIC_PATH

    try:
        pass_mean, min_score, rubric_body = load_rubric(rubric_path)
        deliverables = locate_deliverables(Path(artifact_root), db, job)
        prompt = build_prompt(rubric_body, db, job, deliverables)
        outer = call_claude_cli(prompt, settings_path, claude_bin)

        if outer.get("is_error"):
            raise JudgeError(f"claude CLI reported is_error=true: {outer.get('result')!r}")

        inner_text = outer.get("result")
        if not isinstance(inner_text, str):
            raise JudgeError(f"claude CLI JSON has no string 'result' field: {outer!r}")

        try:
            payload = extract_json_object(inner_text)
        except json.JSONDecodeError as exc:
            raise JudgeError(
                f"could not parse judge response as JSON: {exc}; result={inner_text[:2000]!r}"
            ) from exc

        scores, notes = validate_scores(payload)
    except JudgeError as exc:
        return {"error": str(exc)}, 2

    mean = sum(scores.values()) / len(scores)
    passed = mean >= pass_mean and min(scores.values()) >= min_score
    result = {
        "scores": scores,
        "notes": notes,
        "mean": round(mean, 3),
        "pass": passed,
        "model": outer.get("model"),
        "cost_usd": outer.get("total_cost_usd"),
    }
    return result, (0 if passed else 1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Grade a job's rendered deliverables against ci/llm/rubric.md."
    )
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--job", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    result, code = run_judge(artifact_root=args.artifact_root, db=args.db, job=args.job)
    Path(args.out).write_text(json.dumps(result, indent=2))
    sys.exit(code)


if __name__ == "__main__":
    main()
