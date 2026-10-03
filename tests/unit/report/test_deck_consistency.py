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
            "Steps that need no data migration (ElastiCache, 48%) go first at any confidence"
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
        assert "ElastiCache, 70%" not in text


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
