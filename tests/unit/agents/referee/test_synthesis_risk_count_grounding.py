"""The executive summary must agree with risk_assessment's own counts and severities
(#536).

The summary grounding check (#205/#236) already verifies table/engine attributions,
internal field leaks (#380) and migration order (#393), but it never read risk counts
at all: an LLM run said "two HIGH-severity risks" while ``risk_assessment.risks`` held
1 HIGH and 1 LOW, and the check let it through.

Earlier versions tried to match a risk-count statement broadly and then exclude every
ambiguous reading case by case (an adjective use, a part of the register, an
engine-scoped or wave-scoped count, a conditional or historical figure, a label like
"Phase 1" ...); each fix round found another phrasing that read just as plainly as a
flagged one and was rejected anyway. Rejecting a correct model summary costs more than
missing a real miscount, so ``check_summary_risk_counts`` now does the opposite: it
checks only a small, fixed set of unambiguous sentence shapes (``_OPEN_FRAMES``,
``_RESOLVED`` and ``_LEAVES`` in ``synthesis_grounding.py``), and anything else -- a
wave, a condition, an engine, a label, a historical or projected figure, an adjective
use -- is simply left unchecked. This module's tests are named for that behaviour, not
for which review round found the sentence.
"""

from __future__ import annotations

from src.agents.referee.synthesis_grounding import check_summary_risk_counts


def _risks(*severities: str) -> dict:
    return {
        "overall_risk_level": "HIGH",
        "risks": [{"severity": s, "description": f"risk {i}"} for i, s in enumerate(severities)],
        "resolved_risks": [],
    }


def _with_resolved(risk_assessment: dict, *severities: str) -> dict:
    return {
        **risk_assessment,
        "resolved_risks": [
            {"severity": s, "description": f"resolved {i}"} for i, s in enumerate(severities)
        ],
    }


def _high(findings: list[dict]) -> list[dict]:
    return [f for f in findings if f["high_confidence"]]


# A fixture matching the independent reviews' own test setup: 5 open risks (DynamoDB
# HIGH x2, OpenSearch HIGH, Aurora PostgreSQL LOW, ElastiCache MEDIUM) and 3 resolved.
_FIVE_OPEN_THREE_RESOLVED = _with_resolved(
    _risks("HIGH", "HIGH", "HIGH", "LOW", "MEDIUM"), "MEDIUM", "MEDIUM", "MEDIUM"
)


# ---------------------------------------------------------------------------
# The allowed frames: checked, both when correct and when wrong.
# ---------------------------------------------------------------------------


class TestAllowedFrames:
    """Each of these full-clause shapes is checked; everything else is skipped."""

    def test_there_are_n_risks(self) -> None:
        assert (
            check_summary_risk_counts("There are two open migration risks.", _risks("HIGH", "LOW"))
            == []
        )
        assert _high(
            check_summary_risk_counts(
                "There are three open migration risks.", _risks("HIGH", "LOW")
            )
        )

    def test_there_are_no_open_risks(self) -> None:
        assert check_summary_risk_counts("There are no open risks.", _risks()) == []
        assert _high(check_summary_risk_counts("There are no open risks.", _risks("LOW")))

    def test_n_risks_remain(self) -> None:
        assert (
            check_summary_risk_counts(
                "Three open HIGH risks remain.", _risks("HIGH", "HIGH", "HIGH")
            )
            == []
        )
        assert _high(
            check_summary_risk_counts("Three open HIGH risks remain.", _risks("HIGH", "HIGH"))
        )

    def test_no_severity_risks_remain(self) -> None:
        assert check_summary_risk_counts("No HIGH risks remain.", _risks("LOW")) == []
        assert _high(check_summary_risk_counts("No HIGH risks remain.", _risks("HIGH")))

    def test_a_single_risk_remains(self) -> None:
        assert check_summary_risk_counts("A single HIGH risk remains.", _risks("HIGH")) == []
        assert _high(
            check_summary_risk_counts("A single HIGH risk remains.", _risks("HIGH", "HIGH"))
        )

    def test_colon_breakdown_checks_the_total_and_each_part(self) -> None:
        sentence = "Five risks remain: three HIGH, one LOW and one MEDIUM."
        assert check_summary_risk_counts(sentence, _FIVE_OPEN_THREE_RESOLVED) == []

        # HIGH (3), MEDIUM (1) and the total (5) all still match; only LOW is wrong.
        wrong_part = check_summary_risk_counts(
            sentence, _risks("HIGH", "HIGH", "HIGH", "CRITICAL", "MEDIUM")
        )
        assert len(_high(wrong_part)) == 1
        assert "LOW" in wrong_part[0]["message"]

        wrong_total = check_summary_risk_counts(
            "Two risks remain: one HIGH and one LOW.", _risks("HIGH", "LOW", "MEDIUM")
        )
        assert len(_high(wrong_total)) == 1
        assert "3 risk" in wrong_total[0]["message"]

    def test_parenthetical_matches_the_deterministic_fallbacks_own_wording(self) -> None:
        sentence = "8 open migration risks (overall: LOW); 4 more were resolved by the assignment."
        ra = _with_resolved(_risks(*(["HIGH"] * 8)), *(["LOW"] * 4))
        assert check_summary_risk_counts(sentence, ra) == []

        wrong = _with_resolved(_risks(*(["HIGH"] * 5)), *(["LOW"] * 4))
        findings = check_summary_risk_counts(sentence, wrong)
        assert len(_high(findings)) == 1
        assert "8 open migration risks" in findings[0]["text"]
        assert "5 risk" in findings[0]["message"]

    def test_n_risks_were_resolved(self) -> None:
        ra = _FIVE_OPEN_THREE_RESOLVED  # 3 resolved
        assert check_summary_risk_counts("Three risks were resolved by the assignment.", ra) == []
        assert _high(check_summary_risk_counts("Six risks were resolved by the assignment.", ra))

    def test_resolved_sentence_is_never_checked_against_open_risks(self) -> None:
        ra = _with_resolved(_risks("HIGH", "HIGH"))  # resolved_risks is empty
        findings = check_summary_risk_counts("Two risks were resolved by the assignment.", ra)
        assert len(_high(findings)) == 1
        assert "no risk" in findings[0]["message"]

    def test_resolved_frame_skipped_when_resolved_risks_key_is_missing(self) -> None:
        ra = {"overall_risk_level": "HIGH", "risks": [{"severity": "HIGH", "description": "r"}]}
        assert check_summary_risk_counts("Six risks were resolved by the assignment.", ra) == []

    def test_number_words_one_through_ten(self) -> None:
        for word, n in (
            ("one", 1),
            ("two", 2),
            ("three", 3),
            ("four", 4),
            ("five", 5),
            ("six", 6),
            ("seven", 7),
            ("eight", 8),
            ("nine", 9),
            ("ten", 10),
        ):
            right = _risks(*(["MEDIUM"] * n))
            assert check_summary_risk_counts(f"There are {word} MEDIUM risks.", right) == [], word
            wrong = _risks(*(["MEDIUM"] * (n + 1)))
            assert _high(check_summary_risk_counts(f"There are {word} MEDIUM risks.", wrong)), word

    def test_unrated_severities_count_toward_the_total_but_not_a_known_bucket(self) -> None:
        """A severity string outside CRITICAL/HIGH/MEDIUM/LOW is unrated, never
        folded into LOW (#506)."""
        ra = _risks("SEV-1")
        assert check_summary_risk_counts("There is zero HIGH risks.", ra) == []
        assert check_summary_risk_counts("There is one open risk.", ra) == []
        assert _high(check_summary_risk_counts("There are no open risks.", ra))

    def test_case_insensitive(self) -> None:
        assert (
            check_summary_risk_counts("The migration has one high-severity risk.", _risks("HIGH"))
            == []
        )
        assert (
            check_summary_risk_counts("The migration has one HIGH-severity risk.", _risks("high"))
            == []
        )

    def test_no_summary_or_no_risk_assessment_is_a_no_op(self) -> None:
        assert check_summary_risk_counts("", _risks("HIGH")) == []
        assert check_summary_risk_counts("Two HIGH risks remain.", None) == []


# ---------------------------------------------------------------------------
# Everything else: left unchecked, however the data compares.
# ---------------------------------------------------------------------------


class TestArticlesAndAdjectiveUsesAreNotCounts:
    """An article ("a"/"an") is never itself a count, and "risk(s)" used as an
    adjective or compound noun ("a low risk migration", "five high risk
    queries") never states a count either -- no fixed list of following nouns
    could ever be complete, so a count above one in front of singular "risk"
    simply never matches a frame at all."""

    def test_must_not_flag(self) -> None:
        for sentence in (
            "This is a low risk migration.",
            "Caching introduces a high risk of data loss.",
            "The workload has five high risk queries.",
            "There is a critical risk here.",
            "Review the risk register before cutover.",
            "Caching hot reads reduces risk during peak traffic.",
            "Five high risk access patterns were routed away from DynamoDB.",
            "Two low risk workloads move first.",
        ):
            assert check_summary_risk_counts(sentence, _FIVE_OPEN_THREE_RESOLVED) == [], sentence

    def test_single_is_still_a_real_count(self) -> None:
        """ "single" is the count word; "a" in front of it is just the article."""
        assert check_summary_risk_counts("There is a single HIGH risk.", _risks("HIGH")) == []
        assert _high(
            check_summary_risk_counts("There is a single HIGH risk.", _risks("HIGH", "HIGH"))
        )


class TestPartialCountsAreSkipped:
    """A count for part of the register, not the whole open or resolved list
    or a whole severity bucket, is never one of the allowed frames: "N risks
    remain" always means the WHOLE total or bucket named."""

    def test_must_not_flag(self) -> None:
        for sentence in (
            "The top three risks are hot partitions, scan-heavy reports and cache staleness.",
            "Two risks stand out: hot partitions on orders and unbounded scans.",
            "One risk deserves early attention: the hot partition on the orders table.",
            "The remaining two risks are LOW and MEDIUM.",
            "Only one risk is rated MEDIUM.",
            "At least two HIGH risks need a mitigation plan before cutover.",
            "There are five open migration risks, three of which are rated HIGH.",
        ):
            assert check_summary_risk_counts(sentence, _FIVE_OPEN_THREE_RESOLVED) == [], sentence


class TestEngineScopedCountsAreSkipped:
    """None of the allowed frames mention an engine, so a count attributed to
    one engine, or split across several, never matches one."""

    def test_must_not_flag(self) -> None:
        for sentence in (
            "Three HIGH-severity risks remain, two of them on DynamoDB.",
            "Moving 40 queries to DynamoDB leaves five open risks overall.",
            "DynamoDB carries two HIGH risks, while Aurora PostgreSQL has one LOW risk.",
            "Two HIGH risks sit on DynamoDB and one on OpenSearch.",
            "Two of the three HIGH risks sit on DynamoDB, the third on OpenSearch.",
            "DynamoDB carries two HIGH risks, both tied to hot partitions on the orders table.",
            "Two HIGH risks remain on DynamoDB after the redesign.",
            "No HIGH risks remain on Aurora PostgreSQL.",
        ):
            assert check_summary_risk_counts(sentence, _FIVE_OPEN_THREE_RESOLVED) == [], sentence


class TestWaveConditionalAndHistoricalCountsAreSkipped:
    """A count scoped to a wave, a condition, a time window or a plan ("per
    table group", "if", "once", "without", "would", "before mitigation",
    "last quarter") is not a plain statement about the current whole register,
    so it leaves extra words around the count that no frame allows."""

    def test_must_not_flag(self) -> None:
        for sentence in (
            "Two HIGH risks remain in wave 2 and one in wave 3.",
            "Two HIGH risks remain in wave 2.",
            "Two HIGH risks remain after wave 1 completes.",
            "Two HIGH risks remain per table group.",
            "Two HIGH risks remain in the orders domain.",
            "One HIGH risk remains for the search cutover.",
            "Two HIGH risks remain until write sharding ships.",
            "If the team mitigates two HIGH risks in wave 1, one HIGH risk remains for wave 2.",
            "Once two risks are mitigated, three open risks will remain.",
            "Without the cache tier, six open risks would remain.",
            "Mitigating write hot spots closes two HIGH risks, so one HIGH risk remains.",
            "Before mitigation, four HIGH risks were open; three remain.",
            "Last quarter there were eight open risks.",
            "Wave 2 carries the two HIGH DynamoDB risks.",
            "Phase 2 risks are limited to the search tier.",
            "Consolidating onto fewer engines cut the open risks from eight to five.",
        ):
            assert check_summary_risk_counts(sentence, _FIVE_OPEN_THREE_RESOLVED) == [], sentence


class TestLabelsAreNotCounts:
    """A digit or number word naming an id or a plan step, not a count of
    risks, never matches a frame -- it is never alone with "risk(s)",
    "remain(s)" or "there are/is", only surrounded by other words."""

    def test_must_not_flag(self) -> None:
        for sentence in (
            "Wave 2 risks are limited to the cache tier.",
            "Pattern DDB-AP-3 risk is mitigated by write sharding.",
            "Phase 1 risk is low.",
        ):
            assert check_summary_risk_counts(sentence, _FIVE_OPEN_THREE_RESOLVED) == [], sentence


class TestAmbiguousResolvedMentionsAreSkipped:
    """A clause mentioning "resolved" whose own anchor is not "was/were
    resolved" is not the resolved frame, and -- carrying other words around
    the count -- is not one of the open frames either."""

    def test_must_not_flag(self) -> None:
        for sentence in (
            "Of the eight risks identified, three were resolved by the assignment"
            " and five remain open.",
            "Five risks remain, versus 8 risks before consolidation.",
            "Overall, 5 risks remain open and 3 have been resolved.",
            "Two MEDIUM-severity risks were resolved by moving sessions to ElastiCache.",
        ):
            assert check_summary_risk_counts(sentence, _FIVE_OPEN_THREE_RESOLVED) == [], sentence


class TestLeavesFrameMatchesOnlyAPurposePhrase:
    """ "<subject> leaves/leaving N <severity-or-open> risks (to <verb
    phrase>)" is the frame the real wordpress evidence that opened #536 uses.
    Only a purpose infinitive ("to load-test before cutover") may follow the
    count; any other preposition (in, on, for, per, across, until, after,
    during, within) leaves text the frame cannot consume, and a condition or
    comparison word (if, unless, without, would, could, once, after, before,
    until, should, versus, compared, previously, initially) in the prefix
    before "leaves"/"leaving" also fails it -- "before cutover" in the tail is
    fine, "Before mitigation, ... leaves" in the prefix is not."""

    def test_the_evidence_sentence_is_checked(self) -> None:
        sentence = (
            "The migration removes read pressure from the relational engine and "
            "leaves two HIGH-severity risks to load-test before cutover."
        )
        findings = check_summary_risk_counts(sentence, _risks("HIGH", "LOW"))
        assert len(_high(findings)) == 1
        assert "1 HIGH risk" in findings[0]["message"]
        assert check_summary_risk_counts(sentence, _risks("HIGH", "HIGH")) == []

    def test_must_not_flag(self) -> None:
        for sentence in (
            "leaves two HIGH risks in wave 2",
            "If the team ships the cache first, it leaves one HIGH risk to test.",
            "leaves two HIGH risks for DynamoDB",
        ):
            assert check_summary_risk_counts(sentence, _FIVE_OPEN_THREE_RESOLVED) == [], sentence
