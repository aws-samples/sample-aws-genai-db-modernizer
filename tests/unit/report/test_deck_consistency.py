"""Executive deck consistency (issue #220).

Three defects the rubric judge found on the wordpress validation run:

1. "ElastiCache keep 31.8% of the workload" -- the Assessment Summary footer
   hard-coded the plural verb.
2. The Engine Confidence slide said "Anything under 50% is sequenced last", while
   the Migration Sequencing slide put ElastiCache, at 48%, in Wave 1. ``derive()``
   always puts the no-migration engines first (they are reversible) and applies
   ``CONFIDENCE_FLOOR`` only to migration targets; the sentence now states that rule.
3. "Confirm ElastiCache? ... 1 session store query in the whole workload" -- the
   evidence took the *smallest* of the four signals targeting ElastiCache. It now
   cites the signal the ranking names as the reason the engine was chosen, or the
   engine's largest signal when none is named.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from src.report import pptx_report


def _report(*, reasons: list[str] | None = None) -> dict[str, Any]:
    """Minimal synthesis report: a 48%-confidence cache layer plus two migration
    targets at the floor, the shape of the wordpress validation run."""
    return {
        "database_name": "wordpress",
        "job_id": "job-1",
        "timestamp": "2026-10-03T00:00:00Z",
        "ranking": [
            {
                "target": "elasticache",
                "confidence_score": 48,
                "workload_percent": 31.8,
                "assignment_reason_summary": (
                    reasons
                    if reasons is not None
                    else [
                        "highest confidence for elasticache",
                        "signal override: leaderboard_pattern → elasticache",
                    ]
                ),
            },
            {"target": "dynamodb", "confidence_score": 50, "workload_percent": 58.9},
            {"target": "aurora_mysql", "confidence_score": 50, "workload_percent": 9.3},
        ],
        "recommended_architecture": {
            "databases": [
                {"service": "dynamodb", "table_count": 19},
                {"service": "aurora_mysql", "table_count": 1},
            ]
        },
        "schema_designs": {"elasticache": {"tables": [{}, {}]}},
    }


def _export(signals: list[tuple[str, int, list[str]]]) -> dict[str, Any]:
    return {
        "results": {
            "triage_summary": {
                "signals": [{"signal": n, "query_count": c, "targets": t} for n, c, t in signals]
            }
        }
    }


# The four signals that target ElastiCache on the wordpress run, plus one that does not.
WORDPRESS_SIGNALS = [
    ("key_value_lookups", 37, ["dynamodb", "elasticache", "documentdb"]),
    ("leaderboard_pattern", 15, ["elasticache"]),
    ("high_frequency_reads", 6, ["dynamodb", "elasticache", "documentdb"]),
    ("session_store", 1, ["dynamodb", "elasticache", "documentdb"]),
    ("low_frequency_writes", 22, ["dynamodb", "documentdb"]),
]


def _confirm(f: dict[str, Any]) -> dict[str, Any]:
    return next(d for d in f["decisions"] if d["question"].startswith("Confirm"))


def _all_text(prs) -> str:
    out = []
    for slide in prs.slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                out.append(shape.text_frame.text)
    return "\n".join(out)


def _deck_text(rep: dict[str, Any], exp: dict[str, Any]) -> str:
    f = pptx_report.derive(rep, exp)
    prs = pptx_report.open_deck(keep=1)
    for build in pptx_report.SLIDES:
        build(prs, f)
    return _all_text(prs)


class TestEvidenceSignal:
    def test_cites_the_signal_override_not_the_smallest_signal(self) -> None:
        f = pptx_report.derive(_report(), _export(WORDPRESS_SIGNALS))
        against = _confirm(f)["against"]
        assert "session store" not in against
        assert "15 leaderboard / top-n queries in the whole workload" in against

    def test_falls_back_to_the_largest_signal_without_an_override(self) -> None:
        f = pptx_report.derive(
            _report(reasons=["highest confidence for elasticache"]), _export(WORDPRESS_SIGNALS)
        )
        against = _confirm(f)["against"]
        # key_value_lookups (37) is the largest signal that targets ElastiCache;
        # low_frequency_writes (22) does not target it and must not be picked.
        assert against.split(" · ")[1].startswith("37 ")
        assert "session store" not in against

    def test_single_signal_engine_uses_that_signal(self) -> None:
        f = pptx_report.derive(
            _report(reasons=[]), _export([("session_store", 1, ["elasticache"])])
        )
        assert "1 session store query in the whole workload" in _confirm(f)["against"]

    def test_override_for_another_engine_is_ignored(self) -> None:
        f = pptx_report.derive(
            _report(reasons=["signal override: session_store → dynamodb"]),
            _export(WORDPRESS_SIGNALS),
        )
        assert _confirm(f)["against"].split(" · ")[1].startswith("37 ")

    def test_no_targeting_signal_falls_back(self) -> None:
        f = pptx_report.derive(_report(reasons=[]), _export([]))
        assert "limited supporting evidence" in _confirm(f)["against"]


class TestSequencingRule:
    def test_rule_does_not_claim_everything_under_the_floor_goes_last(self) -> None:
        text = _deck_text(_report(), _export(WORDPRESS_SIGNALS))
        assert "Anything under 50% is sequenced last" not in text

    def test_rule_matches_the_computed_waves(self) -> None:
        rep, exp = _report(), _export(WORDPRESS_SIGNALS)
        f = pptx_report.derive(rep, exp)
        # Wave 1 is the no-migration step, even though it is under the floor ...
        assert [e["engine"] for e in f["waves"][0]["engines"]] == ["elasticache"]
        assert f["conf"]["elasticache"] < pptx_report.CONFIDENCE_FLOOR
        # ... and the stated rule says exactly that.
        text = " ".join(_deck_text(rep, exp).split())
        assert (
            "Steps that need no data migration (ElastiCache at 48%) go first at any confidence"
            in text
        )
        assert "Migration targets under 50% are sequenced last" in text

    def test_low_confidence_migration_target_is_sequenced_last(self) -> None:
        rep = _report()
        rep["ranking"][2]["confidence_score"] = 40  # aurora_mysql below the floor
        f = pptx_report.derive(rep, _export(WORDPRESS_SIGNALS))
        assert [e["engine"] for e in f["waves"][-1]["engines"]] == ["aurora_mysql"]

    def test_no_parenthetical_when_no_migration_step_is_confident(self) -> None:
        rep = _report()
        rep["ranking"][0]["confidence_score"] = 70
        text = " ".join(_deck_text(rep, _export(WORDPRESS_SIGNALS)).split())
        assert "Steps that need no data migration go first" in text
        assert "ElastiCache at 70%" not in text


class TestKeptVerb:
    @pytest.mark.parametrize(
        ("extra_cache", "expected"),
        [
            (False, "ElastiCache keeps 31.8% of the workload with no data migration."),
            (True, "ElastiCache and Aurora MySQL keep"),
        ],
    )
    def test_verb_agrees_with_the_kept_engines(self, extra_cache: bool, expected: str) -> None:
        rep = _report()
        if extra_cache:
            # aurora_mysql no longer a migration target, still carrying workload and
            # with no migration design, reads as Retained: two engines keep their share.
            rep["recommended_architecture"]["databases"] = [
                {"service": "dynamodb", "table_count": 19}
            ]
            rep["schema_designs"]["aurora_mysql"] = {"status": "skipped"}
        text = " ".join(_deck_text(rep, _export(WORDPRESS_SIGNALS)).split())
        assert expected in text
        assert "ElastiCache keep 31.8%" not in text


def _export_with_journeys(
    signals: dict[str, tuple[list[str], dict[str, int]]],
) -> dict[str, Any]:
    """Signals with real query_ids plus the journeys that say where each query
    was effectively assigned. ``signals`` maps name -> (triage targets,
    {assigned engine: number of the signal's queries assigned there})."""
    sigs, items = [], []
    for name, (targets, split) in signals.items():
        ids = []
        for engine, n in split.items():
            for i in range(n):
                qid = f"{name}-{engine}-{i}"
                ids.append(qid)
                items.append({"query_id": qid, "assignment": {"assigned_engine": engine}})
        sigs.append({"signal": name, "query_count": len(ids), "targets": targets, "query_ids": ids})
    return {
        "results": {"triage_summary": {"signals": sigs}},
        "queryJourneys": {"total": len(items), "items": items},
    }


# The run-3 wordpress split of the signals that target ElastiCache, by effective
# assignment (from the query journeys).
RUN3_JOURNEYS = {
    "key_value_lookups": (
        ["dynamodb", "elasticache", "documentdb"],
        {"dynamodb": 23, "elasticache": 14},
    ),
    "leaderboard_pattern": (["elasticache"], {"elasticache": 14, "aurora_mysql": 1}),
    "high_frequency_reads": (["dynamodb", "elasticache", "documentdb"], {"dynamodb": 6}),
    "session_store": (["dynamodb", "elasticache", "documentdb"], {"elasticache": 1}),
}


class TestEvidenceFromEffectiveAssignment:
    def test_override_signal_counts_only_queries_routed_to_the_engine(self) -> None:
        f = pptx_report.derive(_report(), _export_with_journeys(RUN3_JOURNEYS))
        against = _confirm(f)["against"]
        # 14 of the signal's 15 queries landed on ElastiCache; the deck's workload
        # slide shows the 15, so the evidence names both (#256).
        assert "14 of 15 leaderboard / top-n queries routed to ElastiCache" in against

    def test_without_override_picks_the_signal_serving_the_most_queries(self) -> None:
        journeys = dict(RUN3_JOURNEYS)
        journeys["low_frequency_reads"] = (["dynamodb", "documentdb"], {"elasticache": 20})
        f = pptx_report.derive(_report(reasons=[]), _export_with_journeys(journeys))
        # Triage did not target ElastiCache with low_frequency_reads, but the
        # effective assignment routed 20 of its queries there: that is the evidence.
        assert "20 low-frequency read queries routed to ElastiCache" in _confirm(f)["against"]

    def test_signal_served_elsewhere_is_not_evidence(self) -> None:
        journeys = {
            "high_frequency_reads": (["dynamodb", "elasticache"], {"dynamodb": 6}),
            "session_store": (["elasticache"], {"elasticache": 1}),
        }
        f = pptx_report.derive(_report(reasons=[]), _export_with_journeys(journeys))
        against = _confirm(f)["against"]
        assert "high-frequency read" not in against
        assert "1 session store query routed to ElastiCache" in against

    def test_no_double_noun(self) -> None:
        journeys = {"key_value_lookups": (["elasticache"], {"elasticache": 14})}
        f = pptx_report.derive(_report(reasons=[]), _export_with_journeys(journeys))
        against = _confirm(f)["against"]
        assert "14 key-value lookup queries routed to ElastiCache" in against
        assert "lookups queries" not in against


class TestTruncatedJourneys:
    """#228 review: a truncated journey list covers only part of the workload, so
    counting "routed to" from it would undercount. Truncated journeys are treated
    as missing and the evidence falls back to triage targets."""

    def test_truncated_journeys_fall_back_to_triage_targets(self) -> None:
        exp = _export_with_journeys(RUN3_JOURNEYS)
        exp["queryJourneys"]["truncated"] = {"kept": 10, "total": 107, "criterion": "budget"}
        against = _confirm(pptx_report.derive(_report(), exp))["against"]
        assert "15 leaderboard / top-n queries in the whole workload" in against
        assert "routed to" not in against

    def test_untruncated_journeys_still_count_the_effective_assignment(self) -> None:
        exp = _export_with_journeys(RUN3_JOURNEYS)
        against = _confirm(pptx_report.derive(_report(), exp))["against"]
        assert "14 of 15 leaderboard / top-n queries routed to ElastiCache" in against


def _aurora_weakest() -> dict[str, Any]:
    rep = _report(reasons=[])
    rep["ranking"][0]["confidence_score"] = 60
    rep["ranking"][2]["confidence_score"] = 30
    rep["ranking"][2]["assignment_reason_summary"] = []
    return rep


class TestAuroraFamily:
    def test_fallback_matches_aurora_family_triage_target(self) -> None:
        exp = _export([("complex_joins", 7, ["documentdb", "aurora"])])
        f = pptx_report.derive(_aurora_weakest(), exp)
        assert _confirm(f)["question"] == "Confirm Aurora MySQL?"
        assert "7 complex-join queries in the whole workload" in _confirm(f)["against"]

    def test_journeys_match_aurora_flavour(self) -> None:
        exp = _export_with_journeys(
            {"complex_joins": (["documentdb", "aurora"], {"aurora_mysql": 7})}
        )
        f = pptx_report.derive(_aurora_weakest(), exp)
        assert "7 complex-join queries routed to Aurora MySQL" in _confirm(f)["against"]


def _with_retained_documentdb(conf: int) -> dict[str, Any]:
    rep = _report()
    rep["ranking"].append(
        {"target": "documentdb", "confidence_score": conf, "workload_percent": 3.0}
    )
    rep["schema_designs"]["documentdb"] = {"status": "skipped"}
    return rep


class TestSeveralNoMigrationEngines:
    def test_every_no_migration_engine_under_the_floor_is_named(self) -> None:
        text = " ".join(_deck_text(_with_retained_documentdb(44), _export([])).split())
        assert (
            "Steps that need no data migration (ElastiCache at 48%, DocumentDB at 44%) go first"
            in text
        )

    def test_only_the_ones_under_the_floor_are_named(self) -> None:
        text = " ".join(_deck_text(_with_retained_documentdb(80), _export([])).split())
        assert "(ElastiCache at 48%) go first" in text
        assert "DocumentDB at" not in text

    def test_none_under_the_floor(self) -> None:
        rep = _with_retained_documentdb(80)
        rep["ranking"][0]["confidence_score"] = 70
        text = " ".join(_deck_text(rep, _export([])).split())
        assert "Steps that need no data migration go first at any confidence" in text

    def test_three_kept_engines_join(self) -> None:
        rep = _with_retained_documentdb(44)
        rep["recommended_architecture"]["databases"] = [{"service": "dynamodb", "table_count": 19}]
        rep["schema_designs"]["aurora_mysql"] = {"status": "skipped"}
        text = " ".join(_deck_text(rep, _export([])).split())
        assert "ElastiCache, Aurora MySQL and DocumentDB keep" in text


class TestNoMigrationTargets:
    def test_migration_clause_dropped_when_nothing_migrates(self) -> None:
        engines: list[dict[str, Any]] = [{"engine": "elasticache", "role": "Cache layer"}]
        text = pptx_report._sequencing_rule_text(engines, {"elasticache": 48.0})
        assert "Migration targets under" not in text
        assert text.endswith("so they are reversible.")


class TestJoinNames:
    @pytest.mark.parametrize(
        ("names", "expected"),
        [(["A"], "A"), (["A", "B"], "A and B"), (["A", "B", "C"], "A, B and C")],
    )
    def test_join(self, names: list[str], expected: str) -> None:
        assert pptx_report.join_names(names) == expected


class TestRiskMitigationClip:
    """#228 review: the Risk Profile's quoted mitigation is free text from the
    report; an unbounded one overflowed the 1.05in card."""

    def test_long_mitigation_is_clipped_on_a_word_boundary(self) -> None:
        rep = _report()
        long_mit = " ".join(f"step{i} validate the access pattern under load" for i in range(40))
        rep["risk_assessment"] = {
            "mitigation_strategies": [long_mit],
            "risks": [
                {"risk_id": "RISK-001", "severity": "HIGH", "description": "DynamoDB hot key"}
            ],
        }
        text = " ".join(_deck_text(rep, _export([])).split())
        quoted = text.split("Specified mitigation: ", 1)[1].split("\n", 1)[0]
        mit = quoted[: quoted.index("…") + 1]
        assert len(mit) <= pptx_report.MITIGATION_MAX_CHARS + 1
        assert long_mit.startswith(mit[:-1])

    def test_short_mitigation_is_quoted_whole(self) -> None:
        rep = _report()
        rep["risk_assessment"] = {"mitigation_strategies": ["Run load tests first"], "risks": []}
        text = " ".join(_deck_text(rep, _export([])).split())
        assert "Specified mitigation: Run load tests first" in text
        assert "Run load tests first…" not in text


class TestReattributedRiskRow:
    """#222: a risk moved to the engine now serving its queries is described as
    "Flagged by the <Engine> analysis for N queries now on <Engine>: <risk>". The
    Risk Profile row already names the engine, so the attribution is dropped and
    the 78-char clip shows the risk itself."""

    DESC = (
        "[elasticache] Flagged by the DynamoDB analysis for 9 queries now on ElastiCache: "
        "Complex GROUP BY / HAVING aggregations need pre-computed counters "
        "(0% of queries resolved by schema design, 9 remaining)"
    )

    def test_clean_risk_text_drops_the_attribution(self) -> None:
        text, n_q = pptx_report.clean_risk_text(self.DESC)
        assert text == "Complex GROUP BY / HAVING aggregations need pre-computed counters"
        assert n_q == "9"

    def test_risk_row_shows_the_risk_and_its_engine(self) -> None:
        rep = _report()
        rep["risk_assessment"] = {
            "risks": [
                {
                    "risk_id": "RISK-001",
                    "severity": "HIGH",
                    "risk_type": "PERFORMANCE_DEGRADATION",
                    "description": self.DESC,
                    "reattributed_from": "dynamodb",
                }
            ]
        }
        f = pptx_report.derive(rep, _export([]))
        prs = pptx_report.open_deck(keep=1)
        slide = pptx_report.slide_risk(prs, f)
        rows = [
            [c.text for c in row.cells]
            for shape in slide.shapes
            if shape.has_table
            for row in shape.table.rows
        ]
        assert rows[1] == [
            "RISK-001",
            "ElastiCache",
            "Complex GROUP BY / HAVING aggregations need pre-computed counters",
            "9",
        ]

    def test_plain_description_is_unchanged(self) -> None:
        text, _ = pptx_report.clean_risk_text("[dynamodb] Hot partition on wp_options")
        assert text == "Hot partition on wp_options"


class TestSummaryTableTerms:
    """#219 follow-up: the Assessment Summary said "Aurora MySQL: 9 tables" (the
    schema design's target tables) next to a Scope cell of "1 table" (source
    tables mapped to the engine). Each count now names which tables it counts."""

    SUMMARY = (
        "Schema design produced 29 target objects and 65 in-scope access patterns across "
        "29 query groups (dynamodb: 19 tables, 52 access patterns; aurora_mysql: 9 tables, "
        "13 access patterns; aurora_postgresql: 1 table; elasticache: 10 key designs)."
    )

    # The same breakdown as synthesis now writes it (#219): already "target tables".
    NEW_SUMMARY = (
        SUMMARY.replace(": 19 tables", ": 19 target tables")
        .replace(": 9 tables", ": 9 target tables")
        .replace(": 1 table;", ": 1 target table;")
    )

    def _slide(self, summary: str = SUMMARY):
        rep = _report()
        rep["summary_deterministic"] = summary
        f = pptx_report.derive(rep, _export([]))
        return pptx_report.slide_summary(pptx_report.open_deck(keep=1), f)

    def test_summary_names_design_tables_as_target_tables(self) -> None:
        texts = [s.text_frame.text for s in self._slide().shapes if s.has_text_frame]
        summary = next(t for t in texts if "Schema design produced" in t)
        assert "Aurora MySQL: 9 target tables" in summary
        assert "DynamoDB: 19 target tables" in summary
        assert "Aurora PostgreSQL: 1 target table;" in summary
        assert "ElastiCache: 10 key designs" in summary
        assert not re.search(r": \d+ tables?\b", summary)

    def test_new_summary_text_is_left_as_written(self) -> None:
        texts = [
            s.text_frame.text for s in self._slide(self.NEW_SUMMARY).shapes if s.has_text_frame
        ]
        summary = next(t for t in texts if "Schema design produced" in t)
        assert "Aurora MySQL: 9 target tables" in summary
        assert "Aurora PostgreSQL: 1 target table;" in summary
        assert "target target" not in summary

    def test_rewording_is_idempotent(self) -> None:
        once = pptx_report.name_target_tables(self.SUMMARY)
        assert once == self.NEW_SUMMARY
        assert pptx_report.name_target_tables(once) == once

    def test_scope_column_names_source_tables(self) -> None:
        cells = [
            c.text
            for s in self._slide().shapes
            if s.has_table
            for row in s.table.rows
            for c in row.cells
        ]
        assert "1 source table" in cells
        assert "19 source tables" in cells
