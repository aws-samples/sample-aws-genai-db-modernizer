# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The routine-migration artifact as report facts (#1938, ADR-032, ADR-034 Decision 7).

Synthesis reads the newest ``routines/v<N>/routine_migration.json`` at or below the
report's assignment version (:func:`read_routine_facts`) and computes ``facts.routines``
once (:func:`routine_facts`); the routine risks
(``src.shared.migration_risk_rules.routine_risks``), the wave lines
(``src.shared.wave_readiness``) and every deliverable read it.

**Headline counts.** Each routine is in one category, so they add up to the total:

- carried: ``as_is``, ``rewritten`` and ``repaired`` (the artifact's ``model_repaired``,
  never written yet);
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
from src.shared.cutover_message import CUTOVER_MESSAGE

#: What every routine verdict rests on until the load-and-run proof (#1945); the step 1
#: gate's routines check uses the same label.
PROOF_LABEL = "checked, not load-tested"

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
    basis = rewrites + _unknown(row)
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


def proof_sentence(carried: int, unknown: int) -> str:
    """What the carried routines' verdicts rest on: "checked, not load-tested" only for
    the routines whose checks all ran (a check that could not run is never checked);
    nothing when no routine is carried."""
    if carried <= 0:
        return ""  # nothing carried: no routine's checks all ran (#1939)
    if not unknown:
        return f"{PROOF_LABEL.capitalize()}."
    head = f"For {_n(unknown, 'carried routine', 'carried routines')} not every check could run"
    rest = carried - unknown
    if rest <= 0:
        return f"{head}."
    return f"{head}; the other {rest} {'is' if rest == 1 else 'are'} {PROOF_LABEL}."


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


def routine_facts(
    artifact: RoutineMigrationContract | Mapping[str, Any] | None,
    version: int,
    migration_waves: Iterable[Any] | None = None,
) -> dict[str, Any] | None:
    """``facts.routines`` for the routine-migration ``artifact`` read at ``version`` (the
    assignment version it was written for), in the roadmap ``migration_waves``; None
    without an artifact."""
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
    for row in data.get("routines") or []:
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
        "proof": PROOF_LABEL,
        "line": " ".join(
            p
            for p in (
                text,
                proof_sentence(
                    counts["as_is"] + counts["rewritten"] + counts["repaired"], unknown_carried
                ),
            )
            if p
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
    }
    rewrite_kinds = {
        str(w["rule_id"]): str(w.get("rewrite"))
        for row in data.get("routines") or []
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
    return routine_facts(artifact, version, migration_waves)
