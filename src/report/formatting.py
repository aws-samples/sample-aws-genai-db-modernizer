"""Formatting helpers and count labels every synthesis deliverable shares."""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime
from typing import Any

from src.shared.engine_names import ENGINE_DISPLAY_NAMES, display_engine
from src.shared.engines_in_use import retired_engine_savings


def artifact_stem(
    database_name: str,
    artifact: str,
    job_id: str,
    generated: datetime | None = None,
) -> str:
    """Canonical filename stem: ``{database}_{artifact}_{job8}_{YYYYMMDD}``.

    The customer downloads ``1e8ec29e-0227-4ae4-9523-306d4a622c47.html`` and cannot
    tell which database, job or report it is. That is not an S3 problem — the keys
    written here have been descriptive all along — it is that
    ``ArtifactStore.upload_artifact(content, digest, category_type=, file_type=,
    label=)`` takes no filename, so the panel names the download after the artifact
    id. Two things follow: the S3 keys use this stem, and each rendered file states
    its own identity internally (HTML ``<title>`` + ``x-dbmod-*`` meta, Markdown
    front matter, JSON ``_artifact`` envelope) so a UUID download is still traceable.

    Underscore separates fields, hyphen lives inside one, so the stem splits cleanly
    on ``_``. ``job_id[:8]`` rather than the full UUID: 32 bits is ample within one
    database, and 36 characters of UUID would crowd out the two fields that actually
    distinguish the file. The full job id is in the S3 key and inside the file.

    If a future SDK's ``upload_artifact`` accepts a filename, pass this stem to it
    and the panel problem disappears; the SDK is container-only, so that is untested.
    """
    db = re.sub(r"[^a-z0-9]+", "-", database_name.lower()).strip("-") or "database"
    day = (generated or datetime.now(UTC)).strftime("%Y%m%d")
    return f"{db}_{artifact}_{job_id[:8]}_{day}"


def plural_noun(n: Any, singular: str, plural: str | None = None) -> str:
    """The correctly agreed noun for a count, e.g. ``f"{n} {plural_noun(n, 'risk')}"``.

    Every count+noun string across the engineering report and the executive report
    must agree on the English singular/plural rule.
    Before this helper existed, each call site wrote its own — most just hardcoded
    the plural form, which is how the deck printed "1 session store queries"
    (issue #206). Composing the count and the noun is left to the caller (who may
    need ``:,`` thousands separators or other formatting around the number); this
    only decides which form of the word to use.
    """
    try:
        is_one = float(n) == 1
    except (TypeError, ValueError):
        is_one = False
    return singular if is_one else (plural or f"{singular}s")


def plural_verb(n: Any, singular: str, plural: str) -> str:
    """The correctly agreed verb form for a count, e.g.
    ``f"{n} risk {plural_verb(n, 'sits', 'sit')} on dynamodb alone."``.

    English verb agreement is the *opposite* polarity of noun pluralisation: a
    singular subject (``n == 1``) takes the "-s" form ("sits", "is",
    "migrates"), a plural subject takes the bare form ("sit", "are",
    "migrate") -- and irregularly enough (``is``/``are``, ``touches``/
    ``touch``) that there is no safe default to derive one form from the
    other, unlike ``plural_noun``'s "append s". Both forms are always given
    explicitly.

    A dedicated helper rather than reusing ``plural_noun`` for verbs: a couple
    of #206 call sites did exactly that (``plural_noun(n, "sits", "sit")``),
    which happened to produce the right string but reads backwards --
    ``plural_noun``'s positional args mean "singular noun, optional plural
    noun", not "verb form for one, verb form for many".
    """
    try:
        is_one = float(n) == 1
    except (TypeError, ValueError):
        is_one = False
    return singular if is_one else plural


def fmt_num(value: Any, decimals: int = 2) -> str:
    """A metric for display: thousands separators, at most ``decimals`` places.

    Sums of float metrics carry binary noise (``17.460499999999996``); rounding
    here keeps it out of every deliverable (#259). Trailing zeros are dropped
    (``3.0`` -> ``3``); a value that rounds to zero is ``0`` (never ``-0``), and a
    small non-zero one is ``<0.01`` (``>-0.01`` if negative) rather than ``0``.
    NaN and infinities print as an en dash. Anything that is not a number is
    returned as ``str()``.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, int):
        return f"{value:,}"
    if not math.isfinite(value):
        return "\u2013"
    step = 10.0**-decimals
    if value != 0 and abs(value) < step / 2:
        return f"<{step:.{decimals}f}" if value > 0 else f">-{step:.{decimals}f}"
    text = f"{value:,.{decimals}f}"
    text = text.rstrip("0").rstrip(".") if "." in text else text
    return "0" if text in ("-0", "") else text


# A query id as the engineering report's tables name it (#951): its first 8
# characters; report.json keeps the full id. The results page shows 5 (#1028,
# #1031): it is read on screen, where each id opens its full trace.
SHORT_QUERY_ID_CHARS = 8


def short_query_id(query_id: Any) -> str:
    """``22726c84`` for a 64-character query hash; a short id stays as it is."""
    text = str(query_id)
    return text[:SHORT_QUERY_ID_CHARS]


def clip(text: str, n: int) -> str:
    """Truncate on a word boundary — long descriptions and reasons read like a
    rendering bug when cut mid-word ("...DocumentDB versi").

    A single very long word at the cut point (an identifier, path or URL with no
    space to back up to) used to back the whole cut up to the word boundary
    *before* it, silently surrendering most of the character budget to that one
    word (#434 review); hard-cut at ``n`` instead whenever backing up would lose
    more than half of it. The Engineering Report's "Queries on Aurora by
    table" section's joined reasons hit it too (#478).
    """
    text = " ".join(text.split())
    if len(text) <= n:
        return text
    cut = text[:n].rsplit(" ", 1)[0]
    if len(cut) < n // 2:
        cut = text[:n]
    return cut.rstrip(" ,;:.") + "…"


def _fmt_usd(x: Any) -> str:
    return f"${x:,.2f}" if isinstance(x, (int, float)) else "-"


# #380 review: ``current_monthly_cost``/``savings_percent`` are 0.0/0 both when
# the source genuinely has no baseline estimate (the collector reported no RDS
# instance metadata, ``current_cost_known: False``) and -- in principle -- when
# the baseline really is zero; the second never happens in practice, so a bare
# "$0.00"/"0%" reads as a cost comparison that was actually attempted and came
# out even, not as "no comparison was possible". Every deliverable that shows a
# cost baseline or a savings figure must call this, so none of them can show the
# bare number ``build_tco_analysis`` left in place for a reader who only looks
# at the raw field.
#
# #334 review: a report written before ``current_cost_known`` existed has no
# way to say "unknown" explicitly, and a literal ``0`` there is overwhelmingly
# that same "no instance metadata" placeholder, not a genuine zero-cost
# database -- so a missing/``None`` ``current_cost_known`` combined with a
# falsy ``current_monthly_cost`` is unknown too, not just an explicit
# ``current_cost_known: False``. This is the one rule every deliverable
# (the engineering and executive reports here and the React UI's ``tcoAnalysis.js``)
# must share, so they can't disagree on the same report.
def _cost_baseline_text(tco: dict[str, Any]) -> tuple[str, str]:
    """(current monthly cost, savings) display text for ``tco``.

    ``("source cost not provided", "not available")`` when the baseline is
    unknown: ``current_cost_known`` is ``False``, or it is missing/``None``
    and ``current_monthly_cost`` is falsy (covers a report written before
    ``current_cost_known`` existed whose ``current_monthly_cost`` is still a
    literal ``0`` placeholder). A report with no ``current_cost_known`` field
    but a real nonzero ``current_monthly_cost`` keeps showing its number.
    """
    known = tco.get("current_cost_known")
    current = tco.get("current_monthly_cost")
    if known is False or (known is None and not current):
        return "source cost not provided", "not available"
    return (
        _fmt_usd(current),
        f"{fmt_num(tco.get('savings_percent', 0), 1)}%",
    )


def _retired_engine_savings_text(tco: dict[str, Any]) -> str:
    """ "$271.80/mo (ElastiCache), $240.96/mo (OpenSearch)" or "" with none.

    Only engines the source runs today that the plan retires
    (``tco_analysis.retired_engine_savings``, #949), so a saving named for one
    elsewhere in the same report (the reality check's own line, e.g. "Removes
    $271.80/mo by retiring ElastiCache") is traceable to this report's own TCO
    facts. A candidate engine the plan dropped is never listed: the legacy
    ``eliminated_engine_costs`` is not read.
    """
    rows = retired_engine_savings(tco)
    if not rows:
        return ""
    return ", ".join(
        f"{_fmt_usd(row['monthly_cost_usd'])}/mo ({display_engine(row.get('database', ''))})"
        for row in rows
    )


def _completed_designs(report: dict[str, Any]) -> dict[str, dict]:
    """engine -> design summary, only for engines whose design completed."""
    sd = report.get("schema_designs") or {}
    return {e: v for e, v in sd.items() if isinstance(v, dict) and v.get("status") == "completed"}


def access_pattern_scope(report: dict[str, Any]) -> dict[str, tuple[int, int]]:
    """engine -> (in-scope, out-of-scope) access patterns, from ``query_groups``.

    ``schema_designs[engine].access_pattern_count`` counts every pattern, while the
    deterministic summary counts only the in-scope ones; ``query_groups`` carries
    each pattern with its ``in_scope`` flag. A pattern listed in several groups is
    counted once (#255).
    """
    seen: dict[tuple[str, str], bool] = {}
    for g in report.get("query_groups") or []:
        if not isinstance(g, dict):
            continue
        for ap in g.get("access_patterns") or []:
            if isinstance(ap, dict) and ap.get("engine") and ap.get("pattern_id"):
                key = (str(ap["engine"]), str(ap["pattern_id"]))
                seen[key] = seen.get(key, False) or ap.get("in_scope", True) is not False
    out: dict[str, tuple[int, int]] = {}
    for (eng, _), in_scope in sorted(seen.items()):
        n_in, n_out = out.get(eng, (0, 0))
        out[eng] = (n_in + 1, n_out) if in_scope else (n_in, n_out + 1)
    return out


# "<engine>: 15 target tables, 47 access patterns" in the deterministic summary's
# per-engine breakdown. Those counts are in-scope patterns only; the Engineering
# Report heading counts every pattern, so the summary says which it counts (#255).
# Synthesis now writes "in-scope access patterns" itself; this only relabels a
# report.json produced before that.
_PER_ENGINE_APS = re.compile(
    r"\b("
    + "|".join(re.escape(k) for k in sorted(ENGINE_DISPLAY_NAMES, key=len, reverse=True))
    + r"): ([^;()]*?), (\d+) (access patterns?)\b"
)


def label_in_scope_access_patterns(text: str, report: dict[str, Any] | None = None) -> str:
    """``dynamodb: 15 target tables, 47 access patterns`` -> ``..., 47 in-scope access patterns``.

    Only a count that equals the engine's in-scope total in ``report`` (see
    ``access_pattern_scope``) is relabelled, so an older or fallback summary that
    counted something else is never given a label that is not true. Idempotent:
    after rewording, the digits are followed by "in-scope", not "access".
    """
    scope = access_pattern_scope(report or {})

    def sub(m: re.Match[str]) -> str:
        in_scope = scope.get(m.group(1), (None, 0))[0]
        if in_scope is None or int(m.group(3)) != in_scope:
            return m.group(0)
        return f"{m.group(1)}: {m.group(2)}, {m.group(3)} in-scope {m.group(4)}"

    return _PER_ENGINE_APS.sub(sub, text)


# "8 risk(s) identified (overall: LOW; 4 resolved by the assignment)." reads as 4 of
# the 8 being resolved; the 4 are additional risks the assignment removed, listed
# apart from the 8 open ones in the Engineering Report (#258). Synthesis now writes
# "8 open migration risks (...); 4 more were resolved by the assignment." itself,
# which this pattern does not match; it only rewords an older report.json.
_RESOLVED_RISKS = re.compile(
    r"\b(\d+) risk\(s\) identified \(overall: ([^;()]+); (\d+) resolved by the assignment\)\."
)


def label_resolved_risks(text: str) -> str:
    """``8 risk(s) identified (overall: LOW; 4 resolved ...)`` -> ``8 open risk(s) ...; 4 more ...``."""

    def sub(m: re.Match[str]) -> str:
        n = int(m.group(3))
        return (
            f"{m.group(1)} open risk(s) (overall: {m.group(2)}); {n} more "
            f"{plural_verb(n, 'was', 'were')} resolved by the assignment."
        )

    return _RESOLVED_RISKS.sub(sub, text)


# An older summary's "Other targets evaluated: ..." sentence can name engines by raw id
# (#544). Only that sentence is reworded. ``\.(?=\s|$)`` keeps "67.5%" from ending it early.
_OTHER_TARGETS_SENTENCE = re.compile(r"Other targets evaluated: .*?\.(?=\s|$)")
# Only ids in ENGINE_DISPLAY_NAMES are matched; an id without a display name (for example
# ``timestream``) stays raw on purpose.
_ENGINE_ID = re.compile(
    r"\b("
    + "|".join(re.escape(k) for k in sorted(ENGINE_DISPLAY_NAMES, key=len, reverse=True))
    + r")\b"
)


def label_other_targets(text: str) -> str:
    """``documentdb (consolidated into aurora_mysql ...)`` -> ``DocumentDB (consolidated ...)``."""

    def sub(m: re.Match[str]) -> str:
        return _ENGINE_ID.sub(lambda e: display_engine(e.group(1)), m.group(0))

    return _OTHER_TARGETS_SENTENCE.sub(sub, text)


def label_summary_counts(text: str, report: dict[str, Any] | None = None) -> str:
    """Name what each count in the deterministic summary counts (#255, #258, #544)."""
    return label_other_targets(label_resolved_risks(label_in_scope_access_patterns(text, report)))


# #393/#536/#1034: five independent high-confidence checks can reject the generated
# executive summary (``apply_synthesis_llm_output``: ``check_summary_grounding``,
# ``check_summary_internal_leaks``, ``check_summary_wave_order``,
# ``check_summary_risk_counts``, ``check_summary_style``), each stamping its own
# ``[high confidence] ...`` message onto ``summary_validation_warnings`` with its
# own wording -- read here
# instead of hard-coding the table-attribution reason, which used to show even
# when a different check was the one that actually rejected it. The table-
# attribution reason stays last: it is also the default used when none of the
# markers below matches (an older ``report.json`` written before a given check
# existed), so a newly added reason is inserted above it, never appended after.
_FALLBACK_REASONS: tuple[tuple[str, str], ...] = (
    ("internal field", "named one of this codebase's internal field names"),
    # #1034: synthesis_grounding.check_summary_style; the terms stay in the warning.
    ("plain-prose style list", "used wording outside the report's plain-language style"),
    ("migration waves move", "stated a migration order that contradicts the roadmap"),
    (
        "but the risk register has",
        "stated a risk count or severity that doesn't match the risk assessment",
    ),
    (
        "no in-scope query of that engine touches it",
        "named a table under an engine that does not serve it",
    ),
)


def fallback_withheld_reason(report: dict[str, Any]) -> str:
    """Why the generated executive summary was withheld, in one clause.

    Falls back to the original, table-attribution wording when
    ``summary_validation_warnings`` carries none of the known markers (e.g. an older
    ``report.json`` written before a given check existed) -- a missing or unrecognised
    warning should read as the historical default, not an empty note.
    """
    warnings = [str(w) for w in (report.get("summary_validation_warnings") or [])]
    high = [w for w in warnings if w.startswith("[high confidence]")]
    reasons = [text for marker, text in _FALLBACK_REASONS if any(marker in w for w in high)]
    if not reasons:
        reasons = [_FALLBACK_REASONS[-1][1]]
    if len(reasons) == 1:
        return reasons[0]
    return ", ".join(reasons[:-1]) + " and " + reasons[-1]


DASH = "\u2014"


def provenance(
    report: dict[str, Any],
    artifact: str,
    ext: str,
    job_id: str = "",
    source_artifact: str = "",
) -> dict[str, str]:
    """Identity block for one rendered deliverable.

    Carried inside the file (meta tags / front matter / ``_artifact`` envelope) so a
    download named after a UUID is still traceable to a database and a job. See
    ``artifact_stem`` for why the filename itself cannot be set.
    """
    db = str(report.get("database_name") or "database")
    jid = job_id or str(report.get("job_id") or "")
    now = datetime.now(UTC)
    return {
        "artifact": artifact,
        "database": db,
        "job_id": jid,
        "generated": now.isoformat(timespec="seconds"),
        "filename": f"{artifact_stem(db, artifact, jid, now)}.{ext}",
        "source_artifact": source_artifact,
    }
