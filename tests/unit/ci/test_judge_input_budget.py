# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#2146: on a discourse-sized job the judge saw every deliverable section cut
to 0 lines (the facts took the budget first) and scored every criterion a
neutral 3. The deliverables now keep a share of the prompt, every section a
floor, and a judge that would still see an empty section fails before any
model call. No model is called here."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from ci.llm import judge

DB, JOB = "discourse", "d15c0123"

# (heading, lines, characters per line), shaped like a discourse engineering
# report (batch11111454: 336K characters, 81 sections, 14 of them rubric
# sections; the largest are the Migration map, the per-query listings,
# the routines and the DynamoDB trade-offs).
_ENGINE_SECTIONS: list[tuple[str, int, int]] = [
    ("# Database Modernization: Engineering Report", 3, 220),
    ("## Executive summary", 4, 410),
    ("## Target engines", 12, 270),
    ("## Cost", 12, 175),
    ("### Assumptions", 8, 190),
    ("## Migration map (272 tables)", 276, 103),
    ("## Decided automatically, review if needed (5 tables)", 8, 120),
    ("## Close calls (398 queries)", 11, 148),
    ("## Cache layer (ElastiCache)", 4, 250),
    ("## Migration roadmap (3 waves)", 4, 190),
    ("### Wave 1: Move to Aurora PostgreSQL", 7, 430),
    ("### Wave 2: Cache hot reads", 6, 115),
    ("### Wave 3: Move key-value data to DynamoDB", 11, 840),
    ("## Sources of truth and replicas (108 written tables)", 31, 111),
    ("## Target schemas by engine", 0, 0),
    *[(f"### Engine {n} (140 target objects)", 145, 75) for n in range(3)],
    ("## Query groups (290 of 583)", 293, 58),
    ("## Queries on Aurora by table", 0, 0),
    ("### Aurora PostgreSQL (796 queries)", 308, 125),
    ("## Risk posture", 4, 55),
    ("## Risk register (13)", 0, 0),
    *[(f"### Engine {n}", 20, 300) for n in range(4)],
    ("## Resolved by the assignment (17)", 51, 187),
    ("## Mitigation strategies", 5, 160),
    ("## Routines", 1, 88),
    ("### Each routine", 272, 119),
    ("### To do", 271, 29),
    ("## Migration trade-offs (196)", 0, 0),
    *[(f"### Trade-off group {n} ({n + 1})", 14, 280) for n in range(28)],
    ("## Appendix: per-query routing decisions (28 queries)", 9, 96),
    *[(f"### Query decision {n}", 7, 140) for n in range(12)],
]


def _discourse_sized_md() -> str:
    out = []
    for number, (heading, lines, width) in enumerate(_ENGINE_SECTIONS):
        out.append(heading)
        out += [f"s{number} line {i} " + "x" * max(0, width - 14) for i in range(lines)]
    return "\n".join(out) + "\n"


def _discourse_sized_facts() -> dict[str, Any]:
    """About 118K characters, like discourse's: four engines serving 200
    tables each (never cut), 13 risks, three waves listing their tables."""
    tables = [f"public.discourse_table_number_{i:03d}" for i in range(300)]
    return {
        "source": {"report": "report.json"},
        "totals": {"tables": 272, "queries": 1655, "risks": 13},
        "engines": [
            {"engine": f"engine_{n}", "monthly_cost_usd": 100.0 + n, "tables_served": tables[:200]}
            for n in range(4)
        ],
        "tco": {"projected_monthly_cost": 535.89},
        "risks": [
            {
                "id": f"RISK-{n:03d}",
                "description": "d" * 1000,
                "mitigation": "m" * 400,
                "affected_tables": tables[:40],
            }
            for n in range(13)
        ],
        "migration_waves": [
            {"wave": n, "tables": tables, "table_owners": [{"table": t} for t in tables[:100]]}
            for n in range(1, 4)
        ],
    }


def _stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    md: str | None = None,
    pages: list[tuple[str | None, str]] | None = None,
) -> dict[str, Path | None]:
    job = tmp_path / DB / JOB
    (job / "synthesis" / "v3").mkdir(parents=True)
    (job / "synthesis" / "v3" / "report.json").write_text("{}")
    folder = job / "deliverables"
    folder.mkdir()
    (folder / "engineering-report.md").write_text(_discourse_sized_md() if md is None else md)
    (folder / "executive-report.pdf").write_bytes(b"%PDF-1.4 stub")
    if pages is None:  # 16 pages, 23K characters, like discourse's deck
        pages = [
            (f"Page title {n}", " ".join(f"Sentence {n}.{i} " + "p" * 90 + "." for i in range(14)))
            for n in range(16)
        ]
    monkeypatch.setitem(sys.modules, "pypdf", types.ModuleType("pypdf"))
    monkeypatch.setattr(judge, "extract_pdf_pages", lambda path, pypdf: pages)
    monkeypatch.setattr(judge, "build_facts", lambda *a: _discourse_sized_facts())
    return judge.locate_deliverables(tmp_path, DB, JOB)


def _build(deliverables: dict[str, Path | None], **kwargs: Any) -> tuple[str, dict[str, Any]]:
    _, _, rubric_body = judge.load_rubric(judge.DEFAULT_RUBRIC_PATH)
    return judge.build_prompt_with_stats(
        rubric_body, DB, JOB, deliverables, nonce="feedc0de", **kwargs
    )


def _blank_sections(entry: dict[str, Any]) -> list[str]:
    return [
        c["section"]
        for c in entry["sections_truncated"]
        if c["total"] and not c["shown"] and not c.get("partial_line_chars")
    ]


@pytest.mark.parametrize("ceiling", [150_000, judge.PROMPT_CHAR_CEILING])
def test_every_section_of_a_discourse_sized_job_reaches_the_judge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ceiling: int
) -> None:
    """At the old 150K ceiling and the current one: no deliverable is cut
    by dropping lines, and every section with content shows some of it."""
    deliverables = _stage(tmp_path, monkeypatch)
    prompt, stats = _build(deliverables, ceiling=ceiling)
    assert len(prompt) <= ceiling
    assert stats["inputs"]["facts"]["source_chars"] > 110_000  # the discourse shape
    for key in ("pdf", "engineering_md"):
        entry = stats["inputs"][key]
        assert "hard_cut" not in entry, key
        assert _blank_sections(entry) == [], key
        assert entry["chars"] > 0.05 * entry["source_chars"], key
    assert stats["judge_input"] == {"ok": True, "problems": []}
    # The deliverables keep at least half of the room after the fixed parts
    room = ceiling - stats["fixed_chars"]
    shown = stats["inputs"]["pdf"]["chars"] + stats["inputs"]["engineering_md"]["chars"]
    assert shown >= 0.4 * room  # the trusted cut list takes the rest of the half


def test_the_rubric_sections_stay_whole_at_the_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The budget goes where the rubric looks first: the executive summary,
    cost, close calls, roadmap, risk register and mitigations stay whole (on
    discourse the Migration map and the longest wave are cut, last), the
    executive PDF too, while the per-query listings and routines are cut."""
    deliverables = _stage(tmp_path, monkeypatch)
    prompt, stats = _build(deliverables)
    cut = {c["section"] for c in stats["inputs"]["engineering_md"]["sections_truncated"]}
    for whole in (
        "Executive summary",
        "Target engines",
        "Cost",
        "Close calls (398 queries)",
        "Migration roadmap (3 waves) > Wave 1: Move to Aurora PostgreSQL",
        "Risk posture",
        "Risk register (13) > Engine 0",
        "Mitigation strategies",
    ):
        assert whole not in cut, whole
    assert "Queries on Aurora by table > Aurora PostgreSQL (796 queries)" in cut
    assert "Routines > Each routine" in cut
    assert stats["inputs"]["pdf"]["truncated"] is False
    # facts keep every record: the risks list is whole, the table lists are cut
    facts = stats["inputs"]["facts"]
    assert "risks" not in {c["section"] for c in facts["sections_truncated"]}
    assert '"id": "RISK-012"' in prompt


def test_the_old_split_left_the_deliverables_nothing() -> None:
    """The facts used to take their budget first: with discourse's 118K of
    facts in about 127K of room, the deliverables got nothing at all."""
    assert judge.facts_allowance(118_000, 360_000, 127_000) == 63_500
    assert judge.facts_allowance(118_000, 10_000, 127_000) == 117_000
    assert judge.facts_allowance(40_000, 90_000, 127_000) == 40_000  # everything fits
    assert judge.facts_allowance(10, 10, 0) == 0


def test_a_tight_budget_keeps_a_floor_of_every_section() -> None:
    """A protected section used to keep every line while the others were cut
    to a bare heading; now each of them keeps its floor first."""
    text = "## Risk register (3)\n" + "\n".join(f"risk {i} " + "r" * 90 for i in range(200))
    for n in range(10):
        text += f"\n## Appendix {n}\n" + "\n".join(f"a{n} {i} " + "a" * 90 for i in range(20))
    out, info = judge.truncate_sections(text, 8_000, tag="cut-n0")
    assert len(out) <= 8_000
    assert "hard_cut" not in info
    blank = [c for c in info["sections_truncated"] if not c["shown"]]
    assert all(c.get("partial_line_chars") for c in blank), blank
    for n in range(10):
        assert f"a{n} 0 " in out


def test_record_lists_outlast_long_name_lists_in_the_facts() -> None:
    facts = _discourse_sized_facts()
    full = judge.dump_facts(facts)
    out, info = judge.truncate_json(facts, len(full) * 2 // 3, tag="cut-n0")
    parsed = json.loads(out)
    assert len(out) <= len(full) * 2 // 3
    assert len(parsed["risks"]) == 13  # every risk kept
    cut = {c["section"] for c in info["sections_truncated"]}
    assert "migration_waves[].tables" in cut and "risks" not in cut
    assert all(len(e["tables_served"]) == 200 for e in parsed["engines"])  # never cut


# ---------------------------------------------------------------------------
# judge_input: an empty deliverable or section fails, before any model call
# ---------------------------------------------------------------------------


def _stub(tmp_path: Path) -> tuple[Path, Path]:
    called = tmp_path / "called"
    scores = {k: 3 for k in judge.CRITERIA}
    outer = {"is_error": False, "result": json.dumps({"scores": scores, "notes": {}})}
    stub = tmp_path / "fake_claude.py"
    stub.write_text(
        "#!/usr/bin/env python3\nimport sys, pathlib\n"
        "if sys.argv[1:] == ['--help']:\n    sys.exit(0)\n"
        f"pathlib.Path({str(called)!r}).write_text('x')\n"
        f"sys.stdin.read()\nprint({json.dumps(outer)!r})\n"
    )
    stub.chmod(0o755)
    return stub, called


def test_an_empty_engineering_report_fails_without_a_model_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stage(tmp_path, monkeypatch, md="")
    stub, called = _stub(tmp_path)
    result, code = judge.run_judge(
        artifact_root=str(tmp_path), db=DB, job=JOB, claude_bin=str(stub)
    )
    assert code == 2
    assert result["error"].startswith("judge input incomplete")
    assert "engineering_report is empty" in result["error"]
    assert result["judge_input"] == {"ok": False, "problems": ["engineering_report is empty"]}
    assert not called.exists()


def test_sections_the_ceiling_leaves_empty_fail_the_judge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Where even the floors don't fit, the judge says so instead of scoring."""
    deliverables = _stage(tmp_path, monkeypatch)
    _, stats = _build(deliverables, ceiling=40_000)
    check = stats["judge_input"]
    assert check["ok"] is False
    assert any(p.startswith("engineering_report:") for p in check["problems"])


def test_check_judge_input_names_each_problem() -> None:
    def entry(**extra: Any) -> dict[str, Any]:
        return {"source_chars": 100, "chars": 100, "sections_truncated": [], **extra}

    ok = {"facts": entry(), "pdf": entry(), "engineering_md": entry()}
    assert judge.check_judge_input(ok) == {"ok": True, "problems": []}

    blank = {"section": "Risk register (13)", "index": 7, "shown": 0, "total": 9}
    partial = {**blank, "index": 8, "partial_line_chars": [200, 400]}
    heading_only = {**blank, "index": 9, "total": 0}
    bad = {
        "facts": entry(),
        "pdf": entry(source_chars=0, chars=0),
        "engineering_md": entry(
            hard_cut={"lines_dropped": 12}, sections_truncated=[blank, partial, heading_only]
        ),
    }
    assert judge.check_judge_input(bad)["problems"] == [
        "executive_pdf is empty",
        "engineering_report: 12 trailing lines dropped (the section floors do not fit the "
        "prompt ceiling)",
        'engineering_report: 1 sections shown empty: #7 "Risk register (13)"',
    ]
    # A PDF skipped with a note (no pypdf) is said in the prompt: not a problem
    skipped = {**ok, "pdf": entry(source_chars=0, note="pypdf not installed")}
    assert judge.check_judge_input(skipped)["ok"] is True


def test_a_pdf_whose_text_fails_to_extract_fails_the_judge_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The judge would grade without the executive report: fail, before any
    model call. Only a missing pypdf (the dev extra) stays a note."""
    deliverables = _stage(tmp_path, monkeypatch)
    monkeypatch.undo()  # the real pypdf, which can't read the stub PDF
    monkeypatch.setattr(judge, "build_facts", lambda *a: _discourse_sized_facts())
    _, stats = _build(deliverables)
    assert stats["inputs"]["pdf"]["note"].startswith("failed to extract PDF text")
    (problem,) = stats["judge_input"]["problems"]
    assert problem.startswith("executive_pdf: failed to extract PDF text")

    stub, called = _stub(tmp_path)
    result, code = judge.run_judge(
        artifact_root=str(tmp_path), db=DB, job=JOB, claude_bin=str(stub)
    )
    assert code == 2 and "executive_pdf: failed to extract PDF text" in result["error"]
    assert result["judge_input"]["ok"] is False
    assert not called.exists()

    entry = {"source_chars": 0, "chars": 30, "sections_truncated": []}
    md = {"source_chars": 9, "chars": 9, "sections_truncated": []}
    missing = {**entry, "note": judge.PYPDF_MISSING_NOTE + " (requires the 'e2e' extra)"}
    assert judge.check_judge_input({"facts": md, "pdf": missing, "engineering_md": md})["ok"]


def test_the_secondary_sections_get_twice_a_plain_sections_room() -> None:
    """Once the rubric's sections are whole, the trade-offs and the per-engine
    schemas (justified_engines and risks evidence) get SECONDARY_WEIGHT times
    the allowance of a plain section."""
    body = "\n".join(f"line {i} " + "x" * 90 for i in range(40))
    text = "## Risk register (1)\nrisk one\n"
    for heading in ("## Migration trade-offs (40)", "## Target schemas by engine", "## Appendix"):
        text += f"{heading}\n{body}\n"
    out, info = judge.truncate_sections(text, len(text) // 2, tag="cut-n0")
    shown = {c["section"]: c["shown"] for c in info["sections_truncated"]}
    assert "Risk register (1)" not in shown and "risk one" in out
    assert shown["Appendix"] * 98 > judge.SECTION_FLOOR_CHARS  # above the floor
    for secondary in ("Migration trade-offs (40)", "Target schemas by engine"):
        assert shown[secondary] >= 2 * shown["Appendix"] - 1, shown


def test_a_cut_migration_map_keeps_the_multi_engine_rows_first() -> None:
    """The ownership rule checks the tables another engine reads or a cache
    fronts first; a cut keeps those rows before single-engine ones, in
    document order, and says so in the marker and the trusted cut list."""
    rows = []
    for i in range(60):
        also, cache = ("DynamoDB", "—") if i % 3 == 0 else ("—", "—")
        if i == 7:
            cache = "ElastiCache"
        rows.append(f"| `public.t{i:02d}` | Aurora PostgreSQL | {also} | {cache} | `t{i:02d}` |")
    text = (
        "## Migration map (60 tables)\n"
        "Each table has one owner.\n"
        "| Source table | Owner | Also read by | Cached by | Target |\n"
        "|---|---|---|---|---|\n" + "\n".join(rows) + "\n## Appendix\nshort\n"
    )
    out, info = judge.truncate_sections(text, 2_000, tag="cut-n0", protected=())
    kept = [r for r in rows if r in out]
    multi = [r for i, r in enumerate(rows) if i % 3 == 0 or i == 7]
    assert multi and set(multi) <= set(kept)
    assert len(kept) < len(rows)
    assert [r for r in out.splitlines() if r in rows] == kept  # document order
    assert "| Source table |" in out and "|---|" in out and "Each table has one owner." in out
    (cut,) = [c for c in info["sections_truncated"] if c["index"] == 1]
    assert cut["preferred_first"] is True and cut["shown"] == len(kept) + 3
    assert "rows also read by another engine or cached kept first]" in out
    trusted = judge._cut_list({"facts": {}, "pdf": {}, "engineering_md": info})
    assert "rows also read by another engine or cached kept first" in trusted
