# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The routine-migration artifact as report facts (#1938, ADR-032, ADR-034 Decision 7).

Synthesis reads the newest ``routines/v<N>/routine_migration.json`` at or below the
report's assignment version (:func:`read_routine_facts`) and computes ``facts.routines``
once (:func:`routine_facts`); the routine risks
(``src.shared.migration_risk_rules.routine_risks``), the wave lines
(``src.shared.wave_readiness``) and every deliverable read it.

**Headline counts.** Each routine is in one category, so they add up to the total:

- carried: ``as_is``, ``rewritten`` and ``repaired`` (a routine the repair loop carried,
  ``model_repaired`` in ``routine_repair.json`` for the same version, :func:`with_repairs`,
  #2384);
- not carried automatically: ``not_readable`` (the collection user could not read its
  body), ``opted_out`` (the customer turned body collection off), ``not_checked`` (no
  exact ``CREATE`` was collected otherwise), ``not_exact`` (checked on a MySQL ``CREATE``
  rebuilt around a literal) and ``needs_repair`` (a failed check or a dependency reason),
  in that order of precedence.

``unknown_checks_count`` overlaps them: the carried routines with a check that could not
run. An unknown check never changes a routine's status, so ``as_is`` or ``rewritten``
does not mean every check passed (ADR-034).

**Risk classes and severity** (:data:`ROUTINE_RISK_SEVERITY`, maintainer decisions on
#1938, 2026-11-10), deterministic, the same for every engine; each row records what set
its class (``severity_basis``):

==========================  ========  ===================================================
class                       severity  a routine is in it when
==========================  ========  ===================================================
``migrate_yourself``        HIGH      it could not be translated automatically: it needs
                                      repair and a failed check's impact is
                                      ``blocks_move`` or ``needs_rewrite``, or a
                                      dependency reason code is set; or its code could
                                      not be read, collected exactly or checked. A
                                      to-do for the customer that holds no wave
``behaves_differently``     MEDIUM    it needs repair, and every failed check's impact is
                                      ``behaves_differently``
``opted_out``               MEDIUM    it has no code to check: the customer turned body
                                      collection off (information)
``carried_review``          LOW       it is carried with a safe header rewrite (it runs as
                                      the target user), or with a check that could not
                                      run; one risk with a count, the routines listed
                                      here only
==========================  ========  ===================================================

The to-do's wording is one fact: ``migrate_yourself_line`` ("we could not translate N
routines automatically; migrate them yourself before wave N") and
``migrate_yourself_step`` ("migrate these routines yourself before wave N, then check them
again"), with ``wave`` the first wave that brings in the routines' Aurora target. With no
such wave the sentences give no timing and end with the one cutover sentence the texts may
say, :data:`CUTOVER_SENTENCE` ("migrate them yourself; the cutover is your
responsibility"). The risk, the wave line and the wave's gate print them. A row's
``tables_leaving_aurora`` is information only and never a risk.

**What the facts hold.** Ids, kinds, rule ids, reason codes, the collected tables a
routine names, counts, and the catalog's reason and fix hint for each rule the rows name.
Never a routine's text, a diff of it, a hash of it or what a check found in it (the
artifact's ``findings``): the exact text stays in the routine sources (the two-file
decision, ADR-034 Decision 4). Verdicts rest on the static checks, labelled
:data:`PROOF_LABEL` until the load-and-run proof (#1945).

**Tested before deployed** (#2411, the maintainer decision of 2026-11-17 on #1925). Step 1
deploys a carried routine only once the latest proof on a test Aurora for the job found no
difference on the calls made (:func:`proof_hold`). Until a proof has run on the job's
routines file (:func:`proof_is_current`: the proof's ``routine_sources_sha256`` matches the
file's digest), the line says the carried routines are checked but not tested yet and must
be tested on a test Aurora before they are deployed (:func:`not_tested_sentence`); a proof
on another routines file, or one written before the digest, counts as not run. After a
proof, the line gives its counts (:func:`proof_run_sentence`), never more than no
difference on the calls made, and how many step 1 holds back.

**Interventions** (#1940, #1941). ``interventions`` lists each routine the routine repair
loop worked on (``routines/v<N>/routine_repair.json``, the same version as the migration
artifact), so the deliverables say it needed an intervention and what came of it, in the
words :func:`intervention_line` gives (``line``):

- ``model_repaired``: the repair loop's side-by-side comparison accepted the model's
  repair (on a local engine of the source, or with the repair on a test cluster); the
  line says so, and what step 1's deploy gate does with it (``deploy_gate``, #2384):
  before a test it is checked, not tested yet on a test Aurora
  (:func:`repaired_not_tested_sentence`, #2385); ``change_note`` says what changed, in the
  model's own words (untrusted text a renderer escapes); the executive line counts these
  and what step 1 deploys of them (:func:`repaired_executive_clause`);
- ``repaired_pending_proof``: a candidate passed every check, but no proof accepted it,
  so its change note is never printed as what changed;
- ``needs_attention``: the routine could not be rebuilt by position, or every attempt
  failed; ``attempts`` holds each attempt's stage, code and error (words, never text).

A ``model_repaired`` routine is carried, repaired (#2384): ``repaired_count`` counts it and
the test gate decides whether step 1 deploys it, as for any carried routine; its line says
so (``deploy_gate``: ``deployed``, or the reason it is held back). A test counts for it only
when it ran the repair it has now (the proof entry's ``repair_sha256``, the digest of the
redacted candidate, never of the exact text); one that ran another repair, or the original,
holds it back (``rebuild_differs``, ``not_tested``). The other states stay in their
headline category: no figure counts them as carried.

**The words** (#1939). The deliverables print each row's ``status_text`` and ``why``
(:func:`routine_status_text`, :func:`routine_why`: the catalog's reason and fix hint for
a failed check, plain words for each reason, rewrite and check that could not run, never
a code) and the block's ``others_line`` for the routines no row lists, so the engineering
report and the results page say the same; :func:`with_routine_words` fills them in for a
block stored before them.

Nothing in routing reads this module (ADR-034 Decision 8).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

from src.contracts.routine_migration import REASON_CODES, RoutineMigrationContract
from src.contracts.routine_repair import RoutineRepairContract
from src.shared.cutover_message import CUTOVER_MESSAGE

#: What every routine verdict rests on until the load-and-run proof (#1945); the step 1
#: gate's routines check uses the same label.
PROOF_LABEL = "checked, not load-tested"
#: What the verdicts rest on once the routine proof ran on a test cluster (#1945).
PROOF_RUN_LABEL = "load-tested on a test Aurora"
PROOF_OUTCOMES = ("proved", "needs_attention", "not_proved")
#: Why step 1's deploy gate holds a carried routine back (#2411): no current proof tested it,
#: the proof could not prove it, or it found a difference or an error.
HELD_REASONS = ("not_tested", "not_proved", "needs_attention", "depends_on_held", "rebuild_differs")
#: The held-back reason of a routine the test cleared that calls a held one (#2411): its
#: CREATE could fail without it, so the gate leaves it out too.
DEPENDS_ON_HELD = "depends_on_held"
#: The held-back reason of a model-repaired routine whose text step 1 would deploy is not
#: the one the test ran (#2384): the test ran another repair or the original, or the rebuild
#: from the routines file and the repair no longer passes its checks.
REBUILD_DIFFERS = "rebuild_differs"

#: A routine risk class -> its severity, in the order the risks are raised.
ROUTINE_RISK_SEVERITY: dict[str, str] = {
    "migrate_yourself": "HIGH",
    "behaves_differently": "MEDIUM",
    "opted_out": "MEDIUM",
    "carried_review": "LOW",
}

#: A risk's details name at most this many routines, then how many more.
ROUTINE_DETAILS_LISTED = 50

#: The check impacts that keep a routine from being carried as it is.
_BLOCKING_IMPACTS = frozenset({"blocks_move", "needs_rewrite"})
_TEXT_CATEGORIES = ("not_readable", "not_checked", "not_exact")
_CARRIED = {"as_is": "as_is", "rewritten": "rewritten", "model_repaired": "repaired"}
_CATEGORIES = (
    "as_is",
    "rewritten",
    "repaired",
    "needs_repair",
    "not_exact",
    "not_checked",
    "opted_out",
    "not_readable",
)


def _category(row: Mapping[str, Any]) -> str:
    status = str(row.get("status"))
    if status in _CARRIED:
        return _CARRIED[status]
    if row.get("body_status") == "not_readable":
        return "not_readable"
    if row.get("source_text") == "not_collected":
        return "opted_out" if row.get("body_status") == "opted_out" else "not_checked"
    if row.get("source_text") == "not_exact":
        return "not_exact"
    return "needs_repair"


def _classes(
    row: Mapping[str, Any], category: str, impacts: Mapping[str, str | None]
) -> tuple[list[str], list[str]]:
    """``(classes, severity basis)`` for one routine; no class, no risk."""
    reasons = [str(r) for r in row.get("reasons") or []]
    if category == "opted_out":
        return ["opted_out"], reasons
    if category in _TEXT_CATEGORIES:
        return ["migrate_yourself"], reasons
    if category == "needs_repair":
        codes = [r for r in reasons if r in REASON_CODES]
        failed = [r for r in reasons if r not in REASON_CODES]
        # A failed rule the artifact does not describe counts as blocking: never lower.
        blocking = [r for r in failed if impacts.get(r, "blocks_move") in _BLOCKING_IMPACTS]
        if blocking or codes or not failed:
            return ["migrate_yourself"], blocking + codes
        return ["behaves_differently"], failed
    rewrites = [str(w["rule_id"]) for w in row.get("rewrites") or []]
    basis = (["model_repaired"] if category == "repaired" else []) + rewrites + _unknown(row)
    return (["carried_review"], basis) if basis else ([], [])


def _unknown(row: Mapping[str, Any]) -> list[str]:
    return [str(c["rule_id"]) for c in row.get("checks") or [] if c.get("outcome") == "unknown"]


def _n(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _was(n: int) -> str:
    return "was" if n == 1 else "were"


def _line(c: Mapping[str, int], total: int, behaves: int) -> str:
    """The headline sentence: counts only, no routine or table name."""
    carried = c["as_is"] + c["rewritten"] + c["repaired"]
    verb = "carries" if total == 1 else "carry"
    head = f"{carried} of {_n(total, 'routine', 'routines')} {verb} over"
    parts = [
        f"{c['as_is']} as {'it is' if c['as_is'] == 1 else 'they are'}" if c["as_is"] else "",
        f"{c['rewritten']} rewritten" if c["rewritten"] else "",
        f"{c['repaired']} repaired" if c["repaired"] else "",
    ]
    text = head + (": " + ", ".join(p for p in parts if p) if carried else "") + "."
    left = total - carried
    if not left:
        return text
    repair = c["needs_repair"]
    differ = (
        f" ({behaves} of them {'behaves' if behaves == 1 else 'behave'} differently)"
        if behaves
        else ""
    )
    rest = [
        f"{repair} {'needs' if repair == 1 else 'need'} repair{differ}" if repair else "",
        (
            f"{c['not_exact']} {_was(c['not_exact'])} rebuilt around a literal"
            if c["not_exact"]
            else ""
        ),
        f"{c['not_checked']} {_was(c['not_checked'])} not checked" if c["not_checked"] else "",
        (
            f"{c['opted_out']} {'has' if c['opted_out'] == 1 else 'have'} no code collected"
            if c["opted_out"]
            else ""
        ),
        f"{c['not_readable']} {_was(c['not_readable'])} not readable" if c["not_readable"] else "",
    ]
    return (
        text
        + f" {left} {_was(left)} not carried automatically: "
        + ", ".join(p for p in rest if p)
        + "."
    )


#: The one sentence about cutover the texts may say (maintainer rule): no other timing.
#: Mid-sentence form of ``src.shared.cutover_message.CUTOVER_MESSAGE``, its one source (#2062).
CUTOVER_SENTENCE = CUTOVER_MESSAGE[:1].lower() + CUTOVER_MESSAGE[1:].rstrip(".")


def migrate_yourself_lines(count: int, wave: int | None) -> tuple[str | None, str | None]:
    """The to-do's two sentences (maintainer decision on #1938): what happened, and the
    next step; ``(None, None)`` when no routine needs it. Counts only, no name."""
    if not count:
        return None, None
    one = count == 1
    them, these = ("it", "this routine") if one else ("them", "these routines")
    head = f"we could not translate {_n(count, 'routine', 'routines')} automatically"
    if wave:
        return (
            f"{head}; migrate {them} yourself before wave {wave}",
            f"migrate {these} yourself before wave {wave}, then check {them} again",
        )
    # No wave brings in the target: no timing advice, only the cutover sentence.
    return (
        f"{head}; migrate {them} yourself; {CUTOVER_SENTENCE}",
        f"migrate {these} yourself, then check {them} again; {CUTOVER_SENTENCE}",
    )


#: The command that tests the carried routines on a test Aurora (#2411).
TEST_COMMAND = "/test-routines"
#: What a routine is before a test on a test Aurora: never more than checked (#2411).
NOT_TESTED_STATE = "checked, not tested yet"


def repaired_not_tested_sentence(where: str | None = None) -> str:
    """What a model repair the local side-by-side comparison accepted rests on (#2385): it
    was compared on a local engine of the source, never on Aurora, so it is
    :data:`NOT_TESTED_STATE` on a test Aurora, the same words :func:`not_tested_sentence`
    gives the carried routines. Never more than checked."""
    engine = where or "a disposable engine of the source"
    return (
        f"Its repair was compared with the original on {engine}, a local engine of the "
        "source, and gave the same results on the calls compared; it is "
        f"{NOT_TESTED_STATE} on a test Aurora."
    )


def repaired_compared_sentence(where: str | None = None) -> str:
    """Where the repair loop compared a model repair with the original (#2384), never more
    than the calls compared: on a local engine of the source, or side by side with the
    repair on a test cluster (``--proof test-cluster``)."""
    engine = where or "a disposable engine of the source"
    if "test cluster" in engine:
        return (
            f"Its repair was compared side by side with the original on {engine}, and gave "
            "the same results on the calls compared."
        )
    return (
        f"Its repair was compared with the original on {engine}, a local engine of the "
        "source, and gave the same results on the calls compared."
    )


def repaired_executive_clause(repaired_by_model: int, deployed: int | None = None) -> str:
    """The executive line's words for the routines a model repaired (#2385), counts only,
    following step 1's deploy gate (#2384). A repair counts with the routines that can move,
    so the clause names the carried routines, never the ones that need work: before a test
    on the job's routines file (``deployed`` None) they are :data:`NOT_TESTED_STATE` on a
    test Aurora; after one, how many of them step 1 deploys and how many it holds back.
    Nothing when a model repaired none."""
    n = int(repaired_by_model)
    if n <= 0:
        return ""
    one = n == 1
    them, are = ("it", "it is") if one else ("them", "they are")
    head = f"{n} of the carried routines {'was' if one else 'were'} repaired by a model"
    if deployed is None:
        return (
            f"{head} and compared on a local engine of the source: {are} {NOT_TESTED_STATE} "
            "on a test Aurora."
        )
    done = max(0, min(int(deployed), n))
    held = n - done
    if not held:
        return f"{head}, and step 1 deploys {'it' if one else 'all ' + str(n)}."
    if not done:
        return f"{head}; step 1 holds {them} back until a test finds no difference on {them}."
    return f"{head}; step 1 deploys {done} and holds back {held}."


def not_tested_sentence(carried: int, *, checked: bool = True) -> str:
    """The one sentence every deliverable gives while no test has run on the job's routines
    file (#2411): the carried routines are checked, not tested yet, and must be tested on a
    test Aurora (:data:`TEST_COMMAND`) before they are deployed. ``checked`` False leaves out
    "checked," (some carried routine had a check that could not run). Nothing when no
    routine is carried."""
    if carried <= 0:
        return ""
    state = NOT_TESTED_STATE if checked else "not tested yet"
    if carried == 1:
        return (
            f"The carried routine is {state}; test it on a test Aurora with {TEST_COMMAND} "
            "before it is deployed."
        )
    return (
        f"The {carried} carried routines are {state}; test them on a test Aurora with "
        f"{TEST_COMMAND} before they are deployed."
    )


def proof_sentence(carried: int, unknown: int) -> str:
    """What the carried routines' verdicts rest on before any test: which of them had every
    check run (a check that could not run is never checked), then
    :func:`not_tested_sentence`; nothing when no routine is carried."""
    if carried <= 0:
        return ""  # nothing carried: no routine's checks all ran (#1939)
    if not unknown:
        return not_tested_sentence(carried)
    head = f"For {_n(unknown, 'carried routine', 'carried routines')} not every check could run"
    rest = carried - unknown
    checked = f"; the other {rest} {'is' if rest == 1 else 'are'} checked" if rest > 0 else ""
    return f"{head}{checked}. {not_tested_sentence(carried, checked=False)}"


def proof_run_sentence(
    counts: Mapping[str, int], depends_held: int = 0, *, changed: int = 0, untested: int = 0
) -> str:
    """What the routine test on a test Aurora found (#1945), counts only: never more than
    no difference on the calls made, never a claim about every input; then what step 1's
    deploy gate deploys and holds back (#2411): ``depends_held`` of the routines with no
    difference call a held one (:func:`routine_holds`), so the gate holds them back too;
    ``changed`` of them are repairs the test ran another text of (:data:`REBUILD_DIFFERS`),
    and ``untested`` carried routines had no part in the test (a repair made after it,
    #2384)."""
    proved, attention, unproved = (int(counts.get(k, 0)) for k in PROOF_OUTCOMES)
    untested = max(0, int(untested))
    total = proved + attention + unproved + untested
    if not total or total == untested:
        return ""
    parts = [
        f"{proved} showed no difference on the calls made" if proved else "",
        f"{attention} {'needs' if attention == 1 else 'need'} attention" if attention else "",
        f"{unproved} could not be tested" if unproved else "",
        f"{untested} {_was(untested)} not tested" if untested else "",
    ]
    head = f"On a test Aurora, of {_n(total, 'carried routine', 'carried routines')}"
    changed = max(0, min(int(changed), proved))
    depends_held = max(0, min(int(depends_held), proved - changed))
    deployed = proved - depends_held - changed
    held = total - deployed
    reasons = [
        (
            f"{depends_held} because a routine {'it calls' if depends_held == 1 else 'they call'} "
            "is held back"
            if depends_held
            else ""
        ),
        (
            f"{changed} because the repair it has now is not the one tested"
            if changed == 1
            else (
                f"{changed} because the repairs they have now are not the ones tested"
                if changed
                else ""
            )
        ),
    ]
    because = "".join(f", {r}" for r in reasons if r)
    if not held:
        tail = ""
    elif deployed:
        rest = "the other one is" if held == 1 else f"the other {held} are"
        only = "that one" if deployed == 1 else f"those {deployed}"
        if depends_held or changed:
            only = f"{deployed} of them"
        tail = f" Step 1 deploys only {only}; {rest} held back{because}."
    else:
        tail = (
            " Step 1 deploys none of them: "
            + ("it is" if held == 1 else "they are")
            + f" held back{because}."
        )
    return f"{head}: " + "; ".join(p for p in parts if p) + "." + tail


def _and(parts: list[str]) -> str:
    parts = [p for p in parts if p]
    return ", ".join(parts[:-1]) + (" and " if len(parts) > 1 else "") + parts[-1] if parts else ""


def executive_line(
    counts: Mapping[str, int],
    total: int,
    wave: int | None,
    deployed: tuple[int, int] | None = None,
    repaired_by_model: int = 0,
    repaired_deployed: int | None = None,
) -> str:
    """The executive report's routines sentence (with
    :func:`repaired_executive_clause` for ``repaired_by_model``, #2385)."""
    text = _executive_text(counts, total, wave, deployed)
    clause = repaired_executive_clause(repaired_by_model, repaired_deployed)
    return f"{text} {clause}" if clause else text


def _executive_text(
    counts: Mapping[str, int],
    total: int,
    wave: int | None,
    deployed: tuple[int, int] | None,
) -> str:
    """The executive report's routines sentence (the maintainer's direction on #2420,
    2026-11-17): high level, counts only, naming the command. ``counts`` holds the
    headline categories (``as_is``, ``rewritten``, ``repaired``); ``deployed`` is what step
    1's deploy gate deploys and holds back after a current test, ``(deployed, held)``, or
    None before any test. No routine or table name, and no cutover advice."""
    as_is, rewritten, repaired = (int(counts.get(k, 0)) for k in ("as_is", "rewritten", "repaired"))
    carried = as_is + rewritten + repaired
    left = max(0, total - carried)
    when = f" before wave {wave}" if wave else ""
    work = f"{left} {'needs' if left == 1 else 'need'} work{when}" if left else ""
    if not carried:
        head = f"none of your {_n(total, 'routine', 'routines')} can move to Aurora automatically"
        return f"{head}; {work}." if work else f"{head}."
    yours = "your routine" if total == 1 else "your routines"
    kinds = _and([
        f"{as_is} as {'it is' if as_is == 1 else 'they are'}" if as_is else "",
        f"{rewritten} rewritten" if rewritten else "",
        f"{repaired} repaired" if repaired else "",
    ])  # fmt: skip
    text = (
        f"{yours} can move to Aurora with the database: {carried} of {total} for this "
        f"database, {kinds}" + (f"; {work}" if work else "") + "."
    )
    if deployed is None:
        them, are = ("it", "it is") if carried == 1 else ("them", "they are")
        return (
            f"{text} Before {are} deployed, test {them} on a throwaway Aurora with the "
            f"{TEST_COMMAND} command."
        )
    done, held = deployed
    if not held:
        return f"{text} Tested on a throwaway Aurora: step 1 deploys all {done}."
    if not done:
        return (
            f"{text} Tested on a throwaway Aurora: step 1 deploys none of them; "
            f"{held} {'is' if held == 1 else 'are'} held back."
        )
    return (
        f"{text} Tested on a throwaway Aurora: step 1 deploys {done}; "
        f"{held} {'is' if held == 1 else 'are'} held back."
    )


def proof_is_current(run: Any, sources_digest: str | None) -> bool:
    """Whether the artifact's ``proof_run`` ran on the job's routines file as it is now: its
    ``routine_sources_sha256`` is set and equals ``sources_digest`` (#2411). A proof written
    before the digest, or on another routines file, is not current."""
    if not isinstance(run, Mapping):
        run = run.model_dump(mode="json") if run is not None else None
    if not isinstance(run, Mapping) or not sources_digest:
        return False
    return run.get("routine_sources_sha256") == sources_digest


def proof_hold(row: Mapping[str, Any], current: bool) -> str | None:
    """Why step 1's deploy gate holds a carried routine back (:data:`HELD_REASONS`), or
    None when the current proof found no difference on the calls made (``proved``). With
    no current proof, or no proof entry for the row, it is ``not_tested``; a model-repaired
    row (:func:`with_repairs`) counts only a proof of its own repair (``repair_sha256``):
    one of the original is ``not_tested``, one of another repair :data:`REBUILD_DIFFERS`."""
    proof = row.get("proof")
    if not current or not isinstance(proof, Mapping):
        return "not_tested"
    if proof.get("repair_sha256") != row.get("repair_sha256"):
        # The test ran another text than the one step 1 would deploy (#2384).
        return "not_tested" if proof.get("repair_sha256") is None else REBUILD_DIFFERS
    outcome = proof.get("outcome")
    if outcome == "proved":
        return None
    if outcome == "needs_attention":
        return "needs_attention"
    return "not_tested" if proof.get("reason_code") == "not_tested" else "not_proved"


def _calls(row: Mapping[str, Any]) -> set[tuple[str, str]]:
    """The routines a row names, by ``(kind, routine_id)``, as the deploy script orders them."""
    me = (str(row.get("kind")), str(row.get("routine_id")))
    return {
        (str(ref.get("kind")), str(ref.get("routine_id")))
        for d in row.get("dependencies") or []
        if isinstance(d, Mapping)
        for ref in d.get("routines") or []
        if isinstance(ref, Mapping)
    } - {me}


def routine_holds(
    rows: Iterable[Mapping[str, Any]],
    current: bool,
    forced: Mapping[tuple[str, str], str] | None = None,
) -> dict[tuple[str, str], str]:
    """Every carried routine step 1's deploy gate holds back, by ``(kind, routine_id)``,
    with its reason (#2411): its own :func:`proof_hold`, else :data:`DEPENDS_ON_HELD` when it
    calls a held routine, directly or not. The one rule the gate (``routine_test_gate``)
    and ``facts.routines`` both read; a carried routine not listed is deployed. ``rows``
    are the artifact's rows with the repairs on them (:func:`with_repairs`); ``forced``
    holds a routine back whatever its proof says (step 1's rebuild of a repair failed,
    #2384)."""
    carried = [r for r in rows if isinstance(r, Mapping) and r.get("status") in _CARRIED]
    out: dict[tuple[str, str], str] = {}
    for r in carried:
        reason = (forced or {}).get((str(r.get("kind")), str(r.get("routine_id"))))
        reason = reason or proof_hold(r, current)
        if reason is not None:
            out[(str(r.get("kind")), str(r.get("routine_id")))] = reason
    calls = {(str(r.get("kind")), str(r.get("routine_id"))): _calls(r) for r in carried}
    changed = True
    while changed:
        changed = False
        for key, named in calls.items():
            if key not in out and named & out.keys():
                out[key] = DEPENDS_ON_HELD
                changed = True
    return out


def repair_sha256(candidate: str) -> str:
    """The digest of a repair loop candidate (``routine_repair.json``'s ``repaired_create``:
    redacted text, no literal, #2384), which a test of the repair records. Never a digest
    of the exact text (ADR-034 Decision 4)."""
    import hashlib

    return hashlib.sha256(candidate.encode("utf-8")).hexdigest()


def repaired_routines(
    repair: RoutineRepairContract | Mapping[str, Any] | None, version: int
) -> dict[tuple[str, str], str]:
    """Each routine the repair loop carried for routine-migration ``version``
    (``model_repaired``), by ``(kind, routine_id)``, with :func:`repair_sha256` of its
    candidate; empty without a result, or with one for another version (#2384)."""
    if repair is None:
        return {}
    data = repair.model_dump(mode="json") if isinstance(repair, RoutineRepairContract) else repair
    if not isinstance(data, Mapping) or data.get("assignment_version") != version:
        return {}
    return {
        (str(r.get("kind")), str(r.get("routine_id"))): repair_sha256(str(r["repaired_create"]))
        for r in data.get("routines") or []
        if isinstance(r, Mapping)
        and r.get("state") == "model_repaired"
        and r.get("repaired_create")
    }


def repairable_row(row: Mapping[str, Any]) -> bool:
    """Whether a repair may stand in for ``row`` (#2384): it has exact text, and it needs
    attention, or a test refused it (its proof needs attention), or a test already ran a
    repair of it (its proof names one). The repair seam's own rule, plus the repair a test
    ran."""
    if row.get("source_text") != "exact":
        return False
    if row.get("status") == "needs_attention":
        return True
    proof = row.get("proof")
    return isinstance(proof, Mapping) and (
        proof.get("outcome") == "needs_attention" or proof.get("repair_sha256") is not None
    )


def with_repairs(
    rows: Iterable[Mapping[str, Any]], repaired: Mapping[tuple[str, str], str]
) -> list[dict[str, Any]]:
    """The artifact's rows with each repaired routine (:func:`repaired_routines`) as step 1
    and the test carry it (#2384): status ``model_repaired``, with its ``repair_sha256``. Only
    a row the repair loop could have sent (:func:`repairable_row`) is replaced: a row with no
    exact text, or one carried as it is or rewritten that no test refused, never is."""
    out = []
    for r in rows:
        row = dict(r)
        digest = repaired.get((str(row.get("kind")), str(row.get("routine_id"))))
        if digest and repairable_row(row):
            row.update(status="model_repaired", repair_sha256=digest)
        out.append(row)
    return out


def target_wave(target_engine: str, migration_waves: Iterable[Any] | None) -> int | None:
    """The first numbered wave whose engines include ``target_engine``, else None."""
    numbered = []
    for w in migration_waves or []:
        if not isinstance(w, Mapping):
            continue
        n = w.get("wave")
        if isinstance(n, int) and not isinstance(n, bool) and n >= 1:
            if target_engine in {str(e) for e in w.get("engines") or []}:
                numbered.append(n)
    return min(numbered) if numbered else None


#: The repair states the deliverables report as an intervention (#1941).
INTERVENTION_STATES = ("model_repaired", "repaired_pending_proof", "needs_attention")
_STAGE_WORDS = {
    "answer": "no answer",
    "screen": "refused by the repair screen",
    "rebuild": "not rebuilt onto the exact text",
    "rescreen": "refused by the repair screen on the exact text",
    "recheck": "failed the routine checks on the exact text",
    "proof": "the side-by-side proof",
}


def _sentence(text: str) -> str:
    text = " ".join(str(text).split()).rstrip(".")
    return f"{text[:1].upper()}{text[1:]}." if text else ""


def attempt_line(attempt: Mapping[str, Any]) -> str:
    """One attempt of the repair loop in words: its number, where it stopped and why."""
    stage = _STAGE_WORDS.get(str(attempt.get("stage")), str(attempt.get("stage")))
    error = " ".join(str(attempt.get("error") or attempt.get("code") or "").split())
    return f"Attempt {attempt.get('attempt')}: {stage}" + (f": {error}" if error else "")


#: What a model-repaired routine's line says about step 1 (#2384), by its ``deploy_gate``;
#: before a test, after :func:`repaired_not_tested_sentence`.
_NOT_TESTED_WORDS = f"Test it with {TEST_COMMAND} before it is deployed."
_DEPLOY_WORDS: dict[str, str] = {
    "deployed": (
        "On a test Aurora the repair showed no difference on the calls made, so step 1 "
        "deploys it once it rebuilds the repair from the routines file and the repair passes "
        "its checks again there."
    ),
    "not_tested": _NOT_TESTED_WORDS,
    "not_proved": (
        "Step 1 holds it back: the test on a test Aurora could not show it has no difference "
        f"on the calls made ({TEST_COMMAND} names why)."
    ),
    "needs_attention": (
        "Step 1 holds it back: the test on a test Aurora found a difference or an error "
        f"({TEST_COMMAND} names why)."
    ),
    DEPENDS_ON_HELD: "Step 1 holds it back: it calls a routine that is held back.",
    REBUILD_DIFFERS: (
        "Step 1 holds it back: the repair it has now is not the one the test on a test "
        f"Aurora ran; test it again with {TEST_COMMAND}."
    ),
}


def intervention_line(item: Mapping[str, Any]) -> str:
    """What the deliverables say about one intervention (module docstring)."""
    state = item.get("state")
    if state == "model_repaired":
        note = _sentence(str(item.get("change_note") or ""))
        where = item.get("proof_source")
        gate = str(item.get("deploy_gate") or "not_tested")
        # Before a test (or in a report written before #2384): #2385's not-tested words.
        if gate == "not_tested":
            return (
                f"A model repaired it. {repaired_not_tested_sentence(where)} What changed: "
                f"{note} {_NOT_TESTED_WORDS}"
            )
        return (
            f"A model repaired it. {repaired_compared_sentence(where)} What changed: {note} "
            f"{_DEPLOY_WORDS.get(gate, _NOT_TESTED_WORDS)}"
        )
    if state == "repaired_pending_proof" and not item.get("attempts"):
        # A result of the seam alone (#1940): no check ran on the exact text.
        return (
            "A model proposed a repair that no check after the model and no side-by-side "
            "proof has accepted, so it is not carried."
        )
    if state == "repaired_pending_proof":
        return (
            "A model proposed a repair that passed every check again on the exact text, "
            "but no side-by-side proof has accepted it, so it is not carried."
        )
    detail = _sentence(str(item.get("attention_detail") or ""))
    if item.get("attention") in ("not_rebuildable", "signature_mismatch"):
        return f"It was not sent for a model repair: {detail}"
    n = len(item.get("attempts") or [])
    return (
        f"A model repair was tried {n} time{'s' if n != 1 else ''} and none was accepted, so "
        "it needs attention."
    )


def routine_interventions(
    repair: RoutineRepairContract | Mapping[str, Any] | None,
    version: int,
    holds: Mapping[tuple[str, str], str] | None = None,
) -> list[dict[str, Any]]:
    """``facts.routines.interventions`` (#1940, #1941): each routine of the repair
    result for routine-migration ``version`` in :data:`INTERVENTION_STATES`, with its
    reasons, the model's change note, its attempts (words only) and the line the
    deliverables print. Empty without a result, or with one for another version. A
    ``model_repaired`` one also has its ``deploy_gate`` (#2384): ``deployed``, or its reason
    in ``holds`` (:func:`routine_holds`)."""
    if repair is None:
        return []
    data = (
        repair.model_dump(mode="json")
        if isinstance(repair, RoutineRepairContract)
        else dict(repair)
    )
    if data.get("assignment_version") != version:
        return []
    out = []
    for row in data.get("routines") or []:
        if row.get("state") not in INTERVENTION_STATES:
            continue
        item = {
            "kind": row["kind"],
            "routine_id": row["routine_id"],
            "state": row["state"],
            "reasons": list(row.get("reasons") or []),
            "change_note": row.get("change_note") or "",
            "proof_source": row.get("proof_source"),
            "attention": row.get("attention"),
            "attention_detail": row.get("attention_detail"),
            "deploy_gate": (
                (holds or {}).get((str(row["kind"]), str(row["routine_id"])), "deployed")
                if row.get("state") == "model_repaired"
                else None
            ),
            "attempts": [
                {k: a.get(k) for k in ("attempt", "stage", "outcome", "code", "error")}
                for a in row.get("attempts") or []
            ],
        }
        out.append(with_intervention_words(item))
    return out


def with_intervention_words(item: Mapping[str, Any]) -> dict[str, Any]:
    """An intervention with the words the deliverables print: ``line`` and, for a
    routine that still needs attention, ``attempt_lines``."""
    out = dict(item)
    out["line"] = intervention_line(item)
    out["attempt_lines"] = (
        [attempt_line(a) for a in item.get("attempts") or []]
        if item.get("state") == "needs_attention"
        else []
    )
    return out


def routine_facts(
    artifact: RoutineMigrationContract | Mapping[str, Any] | None,
    version: int,
    migration_waves: Iterable[Any] | None = None,
    repair: RoutineRepairContract | Mapping[str, Any] | None = None,
    sources_digest: str | None = None,
    forced: Mapping[tuple[str, str], str] | None = None,
) -> dict[str, Any] | None:
    """``facts.routines`` for the routine-migration ``artifact`` read at ``version`` (the
    assignment version it was written for), in the roadmap ``migration_waves``, with the
    routine repair seam's ``repair`` result for that version when one ran (#1940); None
    without an artifact. ``sources_digest`` is the job's routines file's digest: a proof
    counts only when it ran on that file (:func:`proof_is_current`, #2411). ``forced`` holds
    routines back whatever their proof says (a caller that ran step 1's own rebuild, #2384);
    synthesis passes none, so its facts reflect the test only."""
    if artifact is None:
        return None
    data = (
        artifact.model_dump(mode="json")
        if isinstance(artifact, RoutineMigrationContract)
        else dict(artifact)
    )
    catalog = {str(r["rule_id"]): r for r in data.get("rules") or []}
    impacts = {k: r.get("impact") for k, r in catalog.items()}
    counts = dict.fromkeys(_CATEGORIES, 0)
    kinds: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    classes_n: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    unknown_carried = rewritten_carried = 0
    # A routine the repair loop carried is carried, repaired (#2384): one overlay, read by the
    # counts and the deploy gate's holds alike.
    artifact_rows = with_repairs(data.get("routines") or [], repaired_routines(repair, version))
    for row in artifact_rows:
        category = _category(row)
        counts[category] += 1
        kinds[str(row["kind"])] += 1
        unknown = _unknown(row)
        if category in ("as_is", "rewritten", "repaired"):
            unknown_carried += bool(unknown)
            rewritten_carried += bool(row.get("rewrites"))
        else:
            reasons.update(str(r) for r in row.get("reasons") or [])
        classes, basis = _classes(row, category, impacts)
        if not classes:
            continue
        classes_n[classes[0]] += 1
        rows.append(
            {
                "routine_id": row["routine_id"],
                "kind": row["kind"],
                "status": row["status"],
                "category": category,
                "risk_classes": classes,
                "severity": ROUTINE_RISK_SEVERITY[classes[0]],
                "severity_basis": basis,
                "reasons": list(row.get("reasons") or []),
                "blocked_by": [
                    {"kind": b["kind"], "routine_id": b["routine_id"]}
                    for b in row.get("blocked_by") or []
                ],
                "rewrites": [str(w["rule_id"]) for w in row.get("rewrites") or []],
                "unknown_checks": unknown,
                "notes": list(row.get("notes") or []),
                "tables": sorted(
                    {
                        str(d["name"])
                        for d in row.get("dependencies") or []
                        if d.get("kind") == "table"
                    }
                ),
                "deploy_order": row["deploy_order"],
            }
        )
    named = {r for row in rows for r in (*row["reasons"], *row["rewrites"], *row["unknown_checks"])}
    run = data.get("proof_run")
    current = isinstance(run, Mapping) and proof_is_current(run, sources_digest)
    proved = (
        {k: int((run.get("counts") or {}).get(k, 0)) for k in PROOF_OUTCOMES}
        if isinstance(run, Mapping) and current
        else None
    )
    # What step 1's deploy gate holds back for calling a held routine (#2411): the same rule.
    holds = routine_holds(artifact_rows, current, forced)
    interventions = routine_interventions(repair, version, holds)
    depends_held = sum(1 for reason in holds.values() if reason == DEPENDS_ON_HELD)
    carried_rows = [r for r in artifact_rows if r.get("status") in _CARRIED]
    changed = sum(
        1
        for r in carried_rows
        if holds.get((str(r.get("kind")), str(r.get("routine_id")))) == REBUILD_DIFFERS
        and isinstance(r.get("proof"), Mapping)
        and r["proof"].get("outcome") == "proved"
    )
    untested = sum(1 for r in carried_rows if not isinstance(r.get("proof"), Mapping))
    total = int(data.get("total") or 0)
    text = _line(counts, total, classes_n["behaves_differently"])
    wave = target_wave(str(data["target_engine"]), migration_waves)
    to_do, step = migrate_yourself_lines(classes_n["migrate_yourself"], wave)
    out = {
        "assignment_version": version,
        "source_engine": data["source_engine"],
        "target_engine": data["target_engine"],
        "wave": wave,
        "routine_count": total,
        "carried_count": counts["as_is"] + counts["rewritten"] + counts["repaired"],
        **{f"{c}_count": counts[c] for c in _CATEGORIES},
        "unknown_checks_count": unknown_carried,
        "rewritten_carried_count": rewritten_carried,
        "migrate_yourself_count": classes_n["migrate_yourself"],
        "behaves_differently_count": classes_n["behaves_differently"],
        "carried_review_count": classes_n["carried_review"],
        "migrate_yourself_line": to_do,
        "migrate_yourself_step": step,
        "counts_by_kind": dict(sorted(kinds.items())),
        "reason_counts": dict(sorted(reasons.items())),
        "proof": PROOF_LABEL if proved is None else PROOF_RUN_LABEL,
        "proof_counts": proved,
        "line": " ".join(
            p
            for p in (
                text,
                (
                    proof_sentence(
                        counts["as_is"] + counts["rewritten"] + counts["repaired"], unknown_carried
                    )
                    if proved is None
                    else proof_run_sentence(
                        proved, depends_held, changed=changed, untested=untested
                    )
                ),
            )
            if p
        ),
        "executive_line": executive_line(
            counts,
            total,
            wave,
            (
                None
                if proved is None
                else (
                    counts["as_is"] + counts["rewritten"] + counts["repaired"] - len(holds),
                    len(holds),
                )
            ),
            repaired_by_model=sum(1 for i in interventions if i["state"] == "model_repaired"),
            repaired_deployed=(
                None
                if proved is None
                else sum(1 for i in interventions if i.get("deploy_gate") == "deployed")
            ),
        ),
        "routines": rows,
        "rules": [
            {
                "rule_id": rid,
                "rule": catalog[rid]["rule"],
                "impact": catalog[rid].get("impact"),
                "reason": catalog[rid]["reason"],
                "fix_hint": catalog[rid].get("fix_hint"),
            }
            for rid in sorted(named)
            if rid in catalog
        ],
        "interventions": interventions,
    }
    rewrite_kinds = {
        str(w["rule_id"]): str(w.get("rewrite"))
        for row in artifact_rows
        for w in row.get("rewrites") or []
    }
    return with_routine_words(out, rewrite_kinds)


# ---------------------------------------------------------------------------
# The words every deliverable prints for a routine (#1939)
# ---------------------------------------------------------------------------

#: A headline category -> the routine's status in plain words.
STATUS_WORDS: dict[str, str] = {
    "as_is": "Carried as it is",
    "rewritten": "Carried with a rewrite",
    "repaired": "Carried, repaired",
    "needs_repair": "Not carried: needs repair",
    "not_exact": "Not carried: its code was rebuilt around a literal",
    "not_checked": "Not carried: no exact code was collected",
    "opted_out": "Not carried: no code collected",
    "not_readable": "Not carried: its code could not be read",
}
#: The risk classes whose routines the deliverables list one by one; the carried routines
#: to check (``carried_review``, LOW) roll up into :func:`check_notes` (maintainer
#: decision 2 on #1938).
LISTED_CLASSES = frozenset({"migrate_yourself", "behaves_differently", "opted_out"})
#: A carried-routine note names at most this many routines, then how many more.
NOTE_IDS_LISTED = 10
_CARRIED_CATEGORIES = frozenset({"as_is", "rewritten", "repaired"})
# The categories with no code to check: a check that could not run on them says nothing.
_NO_CODE_CATEGORIES = frozenset({"not_readable", "opted_out", "not_checked"})

# A reason code -> the sentence a routine's ``why`` gives it. Never an id: the routines a
# row names are its ``blocked_by``, which the renderers format as ids.
_WHY_CODE: dict[str, str] = {
    "dependency_cycle": "It is in a dependency cycle.",
    "depends_on_needs_attention": "It names a routine that is not translated.",
    "depends_on_table_not_collected": "It names a table the collection does not have.",
    "depends_on_routine_not_collected": "It names a routine the collection does not have.",
    "source_text_not_collected": "No exact code was collected.",
    "source_text_not_exact": (
        "Its code was rebuilt around a literal, so it is not exact; collect it again to "
        "have it checked and carried."
    ),
}
# ``source_text_not_collected`` by why no code was collected (the row's category).
_WHY_NO_CODE: dict[str, str] = {
    "not_readable": "The collection user could not read its code.",
    "opted_out": "Routine body collection was turned off, so there is no code to check.",
}
#: A rewrite's kind (the artifact's ``rewrite``) -> its carried-routine note's label.
REWRITE_LABELS: dict[str, str] = {
    "definer": "DEFINER set to the target user",
    "owner": "Owner set to the target user",
    "search_path": "search_path pinned in the header",
}
_REWRITE_LABEL = "Safe header rewrite"
_UNKNOWN_LABEL = "A check could not run"


def routine_status_text(row: Mapping[str, Any]) -> str:
    """A row's status in plain words: its headline category, and for a routine that needs
    repair whether it would behave differently."""
    category = str(row.get("category"))
    if category == "needs_repair" and "behaves_differently" in (row.get("risk_classes") or []):
        return "Not carried: would behave differently"
    return STATUS_WORDS.get(category, "Not carried")


def _rule_text(rule: Mapping[str, Any] | None, *, fix: bool) -> str:
    if not isinstance(rule, Mapping):
        return ""
    text = str(rule.get("reason") or "").strip()
    if fix and rule.get("fix_hint"):
        text += f" Fix: {str(rule['fix_hint']).strip()}"
    return f": {text}" if text else ""


def routine_why(row: Mapping[str, Any], rules: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Why a routine that is not carried is in its class, in plain words: each failed
    check with the catalog's reason and fix hint, and each reason code. ``rules`` is
    ``facts.routines.rules`` by rule id. Never a code and never an id (the routines it names
    are ``blocked_by``). What many routines share, a rewrite or a check that could not run,
    is stated once in :func:`check_notes`; a carried row has nothing here."""
    category = str(row.get("category"))
    if category in _CARRIED_CATEGORIES:
        return []
    out: list[str] = []
    for reason in row.get("reasons") or []:
        reason = str(reason)
        if reason == "source_text_not_collected":
            out.append(_WHY_NO_CODE.get(category, _WHY_CODE[reason]))
        elif reason in _WHY_CODE:
            out.append(_WHY_CODE[reason])
        else:
            rule = rules.get(reason)
            impact = rule.get("impact") if isinstance(rule, Mapping) else None
            head = (
                "It would behave differently"
                if impact == "behaves_differently"
                else "A check failed"
            )
            out.append(f"{head}{_rule_text(rule, fix=True) or '.'}")
    return out


def _ref(row: Mapping[str, Any]) -> dict[str, str]:
    return {"kind": str(row.get("kind")), "routine_id": str(row.get("routine_id"))}


def check_notes(
    rows: Iterable[Mapping[str, Any]],
    rules: Mapping[str, Mapping[str, Any]],
    rewrite_kinds: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """What many routines share, once per note (the LOW roll-up, maintainer decision 2 on
    #1938): each rewrite rule and each check that could not run, with its label, the
    catalog's reason once, how many routines it applies to and the first
    :data:`NOTE_IDS_LISTED` of them by kind and id (``more_count`` the rest). A check that
    could not run on a routine with no code collected says nothing and is left out.
    ``rewrite_kinds`` maps a rewrite rule id to its kind (definer, owner, search_path), for
    the label; without it the label is generic."""
    notes: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        no_code = str(r.get("category")) in _NO_CODE_CATEGORIES
        keyed = [("rewrite", str(x)) for x in r.get("rewrites") or []] + [
            ("check_not_run", str(x)) for x in ([] if no_code else r.get("unknown_checks") or [])
        ]
        for kind, rid in keyed:
            if (kind, rid) not in notes:
                rule = rules.get(rid)
                label = (
                    REWRITE_LABELS.get(str((rewrite_kinds or {}).get(rid)), _REWRITE_LABEL)
                    if kind == "rewrite"
                    else _UNKNOWN_LABEL
                )
                notes[(kind, rid)] = {
                    "note": kind,
                    "rule_id": rid,
                    "label": label,
                    "text": (
                        str(rule.get("reason") or "").strip() if isinstance(rule, Mapping) else ""
                    ),
                    "routines": [],
                }
            notes[(kind, rid)]["routines"].append(_ref(r))
    out = []
    for key in sorted(notes, key=lambda k: (k[0] != "rewrite", k[1])):
        note = notes[key]
        refs = note.pop("routines")
        out.append(
            {
                **note,
                "note_count": len(refs),
                "routines": refs[:NOTE_IDS_LISTED],
                "more_count": max(0, len(refs) - NOTE_IDS_LISTED),
            }
        )
    return out


def others_line(total: int, listed: int) -> str | None:
    """The routines no row lists, once: each is carried and every check on it ran and
    passed (a routine with a check that could not run, a rewrite or a reason has a row).
    None when every routine has a row."""
    others = total - listed
    if others <= 0:
        return None
    if not listed:
        if total == 1:
            return "The routine is carried and every check on it passed."
        return f"Each of the {total} routines is carried and every check on it passed."
    if others == 1:
        return "The other routine is carried and every check on it passed."
    return f"The other {others} routines are carried and every check on them passed."


def with_routine_words(
    facts: Mapping[str, Any], rewrite_kinds: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """``facts.routines`` with what the deliverables print (#1939), all derived from its
    own rows and rules: each row's ``status_text``, ``why`` and ``listed`` (the to-do,
    behaves-differently and opted-out rows the deliverables list one by one), the
    ``to_do_routines`` to migrate yourself, the ``check_notes`` the carried routines to
    check roll up into, and ``others_line``. A block stored before them (#1938, or one
    whose fields were emptied by a model round trip) gets the same."""
    out = dict(facts)
    rules = {
        str(r["rule_id"]): r
        for r in facts.get("rules") or []
        if isinstance(r, Mapping) and r.get("rule_id")
    }
    rows = [r for r in facts.get("routines") or [] if isinstance(r, Mapping)]
    out["routines"] = [
        {
            **r,
            "status_text": routine_status_text(r),
            "why": routine_why(r, rules),
            "listed": bool(LISTED_CLASSES & set(r.get("risk_classes") or [])),
        }
        for r in rows
    ]
    out["to_do_routines"] = [
        _ref(r) for r in rows if "migrate_yourself" in (r.get("risk_classes") or [])
    ]
    out["check_notes"] = check_notes(rows, rules, rewrite_kinds)
    out["others_line"] = others_line(int(facts.get("routine_count") or 0), len(rows))
    if not out.get("executive_line") and "as_is_count" in facts:
        # A block stored before #2411: no test was current then, so the before-test form.
        out["executive_line"] = executive_line(
            {k: int(facts.get(f"{k}_count") or 0) for k in ("as_is", "rewritten", "repaired")},
            int(facts.get("routine_count") or 0),
            facts.get("wave") if isinstance(facts.get("wave"), int) else None,
            repaired_by_model=sum(
                1
                for i in facts.get("interventions") or []
                if isinstance(i, Mapping) and i.get("state") == "model_repaired"
            ),
        )
    out["interventions"] = [
        with_intervention_words(i)
        for i in facts.get("interventions") or []
        if isinstance(i, Mapping)
    ]
    return out


def needs_routine_words(facts: Any) -> bool:
    """Whether a stored ``facts.routines`` lacks the words (#1939): missing, or empty where
    its own counts say there is something (a model round trip fills in None and [])."""
    if not isinstance(facts, Mapping):
        return False
    rows = [r for r in facts.get("routines") or [] if isinstance(r, Mapping)]
    if any(not r.get("status_text") or "listed" not in r for r in rows):
        return True
    if "check_notes" not in facts or "to_do_routines" not in facts:
        return True
    if int(facts.get("migrate_yourself_count") or 0) and not facts.get("to_do_routines"):
        return True
    if int(facts.get("carried_review_count") or 0) and not facts.get("check_notes"):
        return True
    if any(isinstance(i, Mapping) and not i.get("line") for i in facts.get("interventions") or []):
        return True
    if not facts.get("executive_line") and "as_is_count" in facts:
        return True
    return not facts.get("others_line") and int(facts.get("routine_count") or 0) > len(rows)


def read_routine_facts(
    store: Any,
    database_name: str,
    job_id: str,
    assignment_version: int,
    migration_waves: Iterable[Any] | None = None,
) -> dict[str, Any] | None:
    """``facts.routines`` from the newest routine-migration artifact at or below
    ``assignment_version``; None when the job has none (no routines, a job saved before
    the artifact, or a report with no assignment version)."""
    from src.storage.routine_migration_store import latest_routine_migration

    if assignment_version < 1:
        return None
    found = latest_routine_migration(store, database_name, job_id, assignment_version)
    if found is None:
        return None
    version, artifact = found
    from src.agents.collector.routine_sources import routine_sources_digest
    from src.storage.routine_repair_store import read_routine_repair

    repair = read_routine_repair(store, database_name, job_id, version)
    digest = routine_sources_digest(store, database_name, job_id)
    # The facts reflect the test (#2384): step 1's own rebuild of a repair, which runs the
    # routine checks, never runs here, since synthesis never reaches them (ADR-034
    # Decision 8); the deployed line says step 1 checks the repair again before it deploys.
    return routine_facts(artifact, version, migration_waves, repair, digest)
