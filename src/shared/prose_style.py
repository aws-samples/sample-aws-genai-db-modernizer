"""Plain-prose style list for customer deliverables (#1034).

Customer deliverables (the engineering report, the PDF report, the UI-facing
fields of ``report.json``) should read as written by a person. This module is
the single list of the machine-writing tells we keep out of them, and the one
place every check finds them:

- the em dash (``—``, spaced or not) and its ASCII stand-in, a spaced double
  hyphen (`` -- ``). The en dash (``–``) is the range mark ("9 am – 5 pm",
  ``30s–60s``) and is allowed everywhere, as is a lone dash used as an
  empty-value placeholder (``| — |``, ``| -- |``);
- filler words (``robust``, ``seamless``, ``leverage``, ``ensure``, ...);
- hedges (``not the only one``, ``typically``, ``generally`` (but not
  "generally available"), ``might``, ``could potentially``, ``if that matters``,
  ``where possible``, and ``a moment``, ``briefly``, ``for a short time``,
  ``may see the old value``, ``typical values are far smaller`` unless the
  sentence states the bound: a number with a unit, ``until``, ``up to``), and
  the figureless estimates ``usually``, ``likely`` and ``roughly`` (``roughly``
  before a number is a stated estimate and stays);
- constructions (``not just/not only ... but``, ``it's worth noting``,
  ``here's``, ``let's``);
- an all-caps severity label inside prose (``CRITICAL/HIGH``, ``HIGH risks``);
  write ``critical or high`` instead. A word such as "LOW latency" is not a
  severity label.

Text that is not prose is never matched or rewritten (``_protected_spans``, the
one Markdown tokenizer both ``find_style_hits`` and ``replace_dashes`` use):
fenced code blocks and code spans by CommonMark's rules (backtick and tilde
fences, an unclosed fence running to the end of the text, multi-backtick
spans), URLs and file paths, identifiers (snake_case, kebab-case, CamelCase,
dotted names), placeholder table cells, and any ``identifiers`` the caller
passes (the collected table and column names, so a customer table named
``journeys`` never counts as the word "journeys").

Deterministic templates are fixed at their source so they never produce a hit.
Model-written prose goes through ``clean_model_prose`` at the LLM seam that
writes it: an em dash is rewritten only where that cannot change a fact
(``replace_dashes``: one dash in a sentence becomes a comma or colon, two
become parentheses; it never drops or reorders a word, digit, ``$`` or ``%``),
and any other hit, including a dash it left alone, sends the
field back to the deterministic text, with the reason recorded in the trace,
not in the deliverable. Renderers never filter (ADR-032).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

# Each filler word with the inflections a sentence would use. Matched as whole
# words, case-insensitively. Not "landscape" (a page orientation), "elevated"
# ("elevated latency", "elevated privileges") nor "unlock" (row locks).
FILLER_WORDS: tuple[str, ...] = (
    "robust",
    "seamless",
    "seamlessly",
    "leverage",
    "leverages",
    "leveraged",
    "leveraging",
    "ensure",
    "ensures",
    "ensured",
    "ensuring",
    "comprehensive",
    "comprehensively",
    "crucial",
    "crucially",
    "pivotal",
    "delve",
    "delves",
    "delving",
    "holistic",
    "holistically",
    "streamline",
    "streamlines",
    "streamlined",
    "streamlining",
    "empower",
    "empowers",
    "empowered",
    "empowering",
    "elevate",
    "elevates",
    "elevating",
    "journey",
    "journeys",
    "furthermore",
    "moreover",
    "notably",
    "importantly",
    "essentially",
    "ultimately",
    "truly",
)

HEDGES: tuple[str, ...] = (
    "not the only one",
    "typically",
    "generally",
    "might",
    "could potentially",
    # Vague softeners on an effect or a limit (#1109). Each is a hedge only
    # while the sentence states no bound; see ``_BOUNDED_HEDGES``.
    "a moment",
    "briefly",
    "if that matters",
    "for a short time",
    "where possible",
    "typical values are far smaller",
    "may see the old value",
    # Soft estimates that carry no figure (#1109, from a real model run).
    # "roughly" is allowed before a number, which is a stated estimate.
    "roughly",
    "usually",
    "likely",
)

# (label, pattern): constructions that read as machine-written.
CONSTRUCTIONS: tuple[tuple[str, str], ...] = (
    ("not just/not only ... but", r"\bnot (?:just|only)\b[^.;:!?]*?\bbut\b"),
    ("it's worth noting", r"\bit(?:'|’)?s worth noting\b|\bit is worth noting\b"),
    ("here's", r"\bhere(?:'|’)s\b"),
    ("let's", r"\blet(?:'|’)s\b"),
)

SEVERITY_LABELS: tuple[str, ...] = ("CRITICAL", "HIGH", "MEDIUM", "LOW")

# --- the shared Markdown tokenizer: spans that are not prose -----------------

_FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def _code_spans(text: str) -> list[tuple[int, int]]:
    """Fenced code blocks and code spans, by CommonMark's rules: a fence is a
    line starting with three or more backticks or tildes, closed only by a
    line of the same character at least as long (an unclosed fence runs to
    the end of the text); a code span opens with a run of N backticks and
    closes at the next run of exactly N (no closing run: the backticks are
    literal)."""
    spans: list[tuple[int, int]] = []
    lines = text.splitlines(keepends=True)
    offsets = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line)
    outside: list[tuple[int, int]] = []  # text outside fences, for code spans
    i = 0
    seg_start = 0
    while i < len(lines):
        m = _FENCE_OPEN_RE.match(lines[i])
        if m and not (m.group(1)[0] == "`" and "`" in lines[i][m.end() :]):
            fence_char, fence_len = m.group(1)[0], len(m.group(1))
            close = re.compile(
                r"^ {0,3}" + re.escape(fence_char) + "{" + str(fence_len) + r",}\s*$"
            )
            j = i + 1
            while j < len(lines) and not close.match(lines[j]):
                j += 1
            start = offsets[i]
            end = offsets[j] + len(lines[j]) if j < len(lines) else len(text)
            outside.append((seg_start, start))
            spans.append((start, end))
            seg_start = end
            i = j + 1
            continue
        i += 1
    outside.append((seg_start, len(text)))
    for a, b in outside:
        k = a
        while k < b:
            if text[k] != "`":
                k += 1
                continue
            run_end = k
            while run_end < b and text[run_end] == "`":
                run_end += 1
            n = run_end - k
            close_at = -1
            s = run_end
            while s < b:
                if text[s] == "`":
                    e = s
                    while e < b and text[e] == "`":
                        e += 1
                    if e - s == n:
                        close_at = e
                        break
                    s = e
                else:
                    s += 1
            if close_at == -1:
                k = run_end  # no closing run: literal backticks
                continue
            spans.append((k, close_at))
            k = close_at
    return spans


_NOT_PROSE_RES = (
    re.compile(r"\b(?:https?|s3|ftp)://\S+", re.IGNORECASE),  # URL
    re.compile(r"(?<![\w/])(?:~|\.{1,2})?/[\w.\-/]+"),  # absolute or ./ path
    re.compile(r"\b[\w.-]+/[\w./-]*\.[A-Za-z]\w{0,4}\b"),  # relative path with an extension
    re.compile(r"\b\w*_\w*\b"),  # snake_case (any token with an underscore)
    re.compile(r"\b[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+\b"),  # kebab-case
    re.compile(r"\b[a-z]+[A-Z][A-Za-z0-9]*\b|\b[A-Z][a-z0-9]+[A-Z][A-Za-z0-9]*\b"),  # CamelCase
    re.compile(r"\b[A-Za-z]\w*(?:\.\w+)+\b"),  # dotted name (schema.table)
    re.compile(r"^\s*(?:—|--|-)\s*$|(?<=\|)\s*(?:—|--|-)\s*(?=\|)", re.MULTILINE),  # placeholder
)


def _protected_spans(
    text: str, identifiers: Iterable[str] = (), *, code_only: bool = False
) -> list[tuple[int, int]]:
    """Merged ``(start, end)`` spans of ``text`` that are not prose: code
    (``_code_spans``) and, unless ``code_only``, URLs, paths, identifiers,
    placeholder cells and the given ``identifiers`` (``code_only`` is for the
    severity check, where "HIGH-severity" is a label, not a kebab-case name)."""
    spans = _code_spans(text)
    if not code_only:
        spans += [(m.start(), m.end()) for rx in _NOT_PROSE_RES for m in rx.finditer(text)]
        given = {n.strip() for n in identifiers if n and n.strip()}
        names = sorted(given | {n.rsplit(".", 1)[-1] for n in given}, key=len, reverse=True)
        if names:
            rx = re.compile(
                r"(?<![\w-])(?:" + "|".join(re.escape(n) for n in names) + r")(?![\w-])",
                re.IGNORECASE,
            )
            spans += [(m.start(), m.end()) for m in rx.finditer(text)]
    spans.sort()
    merged: list[tuple[int, int]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def _inside(pos: int, spans: list[tuple[int, int]]) -> bool:
    return any(s <= pos < e for s, e in spans)


def _blank(text: str, spans: list[tuple[int, int]]) -> str:
    """``text`` with every protected span replaced by spaces (offsets kept)."""
    out = list(text)
    for s, e in spans:
        out[s:e] = " " * (e - s)
    return "".join(out)


# --- dashes -----------------------------------------------------------------

# The banned dash: an em dash, spaced or not, and its ASCII stand-in, a spaced
# double hyphen. The en dash is the range mark ("9 am – 5 pm", "30s–60s") and
# is allowed everywhere; so is a spaced single hyphen.
_EM_DASH_RE = re.compile(r"[ \t]*—[ \t]*|(?<=\S)[ \t]+--[ \t]+(?=\S)")


def _em_dashes(text: str, spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """``(start, end)`` of every banned dash outside the protected spans, the
    spaces around it included."""
    out = []
    for m in _EM_DASH_RE.finditer(text):
        core = m.start() + (len(m.group(0)) - len(m.group(0).lstrip()))
        if not _inside(core, spans):
            out.append((m.start(), m.end()))
    return out


# Where a sentence ends for the rewrite: a newline, a semicolon, or a full stop,
# question or exclamation mark followed by a space.
_UNIT_END_RE = re.compile(r"\n|;|[.!?](?=\s|$)")


def replace_dashes(text: str, identifiers: Iterable[str] = ()) -> str:
    """Rewrite the banned dash in model text only where that cannot change a
    fact. Within one sentence on one line (a newline, ``;`` or sentence end
    closes it), with text on both sides of each dash:

    - exactly one dash: ``", "``, or ``": "`` when what follows is a number or
      a list (``"capacity—not write"`` -> ``"capacity, not write"``);
    - exactly two: parentheses (``"A — b — c"`` -> ``"A (b) c"``).

    Any other dash is left as it is, so it stays a hit and the seam sends the
    field back to the deterministic text. Code, URLs, identifiers and en
    dashes are never touched; no word, digit, ``$`` or ``%`` is ever dropped or
    reordered.
    """
    if not text:
        return text
    spans = _protected_spans(text, identifiers)
    dashes = _em_dashes(text, spans)
    if not dashes:
        return text
    prose = _blank(text, spans)
    bounds = [0] + [m.end() for m in _UNIT_END_RE.finditer(prose)] + [len(text)]
    edits: list[tuple[int, int, str]] = []
    for u0, u1 in zip(bounds, bounds[1:], strict=False):
        inner = [d for d in dashes if u0 <= d[0] and d[1] <= u1]
        if not inner or not all(
            text[u0 : d[0]].strip()
            and text[d[1] : u1].strip(".!?;\n ").strip()
            and not text[u0 : d[0]].rstrip().endswith(("(", "["))
            and not text[d[1] : u1].lstrip().startswith((")", "]"))
            for d in inner
        ):
            continue
        if len(inner) == 1:
            s, e = inner[0]
            rest = text[e:u1]
            listy = bool(re.match(r"\$?\d", rest)) or "," in rest
            edits.append((s, e, ": " if listy else ", "))
        elif len(inner) == 2 and text[inner[0][1] : inner[1][0]].strip():
            (s1, e1), (s2, e2) = inner
            after = text[e2 : e2 + 1]
            edits.append((s1, e1, " ("))
            edits.append((s2, e2, ")" + ("" if not after or after in ".,;:!?)" else " ")))
    out = text
    for s, e, rep in sorted(edits, reverse=True):
        out = out[:s] + rep + out[e:]
    return out


# --- the matcher ------------------------------------------------------------

_FILLER_RE = re.compile(r"\b(" + "|".join(FILLER_WORDS) + r")\b", re.IGNORECASE)
# Two-word hedges are kept as word lists and joined here, so no prose pattern
# holds a SQL keyword as a regex literal (the SQL-detector guard, #893).
_WORD_HEDGES: tuple[tuple[str, ...], ...] = (("where", "possible"),)
_WORD_HEDGE_PATTERN = "|".join(r"\s+".join(words) for words in _WORD_HEDGES)
_HEDGE_RE = re.compile(
    r"\b(not\s+the\s+only\s+one|typically|generally(?!\s+available)|might"
    r"|could\s+potentially"
    r"|if\s+(?:that|it|this)\s+matters|usually|likely"
    r"|roughly(?!\s+[~$]?\d)|" + _WORD_HEDGE_PATTERN + r")\b",
    re.IGNORECASE,
)
_ABBREVIATION_RE = re.compile(r"\b(?:e\.g|i\.e|etc|vs|approx)\.", re.IGNORECASE)
# Hedges on the size of an effect or limit. They are kept when the same
# sentence states the bound (rubric rule, #1075: "stale until the TTL expires",
# "up to about 1 s behind", "a read can miss a write for 2 s"), and rejected
# when it does not ("can take a moment", "briefly stale").
_BOUNDED_HEDGE_RE = re.compile(
    r"\b(a\s+moment|briefly|for\s+a\s+(?:short|brief)\s+(?:time|while|period)"
    r"|typical\s+values\s+are\s+(?:far|much)\s+smaller"
    r"|may\s+see\s+the\s+(?:old|stale|previous)\s+value)\b",
    re.IGNORECASE,
)
_BOUND_RE = re.compile(
    r"\buntil\b|\bup\s+to\b|\bwithin\b|\bat\s+most\b"
    r"|\b\d+(?:\.\d+)?\s*(?:ms|milliseconds?|s|secs?|seconds?|minutes?|mins?|hours?|h|days?|%)(?!\w)",
    re.IGNORECASE,
)


def _states_a_bound(text: str, start: int, end: int) -> bool:
    """True when the sentence around ``text[start:end]`` states a bound. The
    full stop of "e.g.", "i.e." and "etc." does not end a sentence."""
    text = _ABBREVIATION_RE.sub(lambda m: m.group(0)[:-1] + "_", text)
    left = max(text.rfind(". ", 0, start), text.rfind("\n", 0, start), text.rfind(";", 0, start))
    rights = [
        i for i in (text.find(". ", end), text.find("\n", end), text.find(";", end)) if i >= 0
    ]
    right = min(rights) if rights else len(text)
    return bool(_BOUND_RE.search(text[left + 1 : right]))


_CONSTRUCTION_RES = tuple((label, re.compile(p, re.IGNORECASE)) for label, p in CONSTRUCTIONS)
_SEV = "|".join(SEVERITY_LABELS)
# A severity label: two of them paired ("CRITICAL/HIGH", "HIGH or CRITICAL"),
# one followed by a risk or severity word ("HIGH risks", "HIGH-severity"), or
# one after "severity". Not "LOW latency" or a badge alone in a table cell.
_RISK_WORD = r"(?:risks?|severity|severities|priority|issues?|findings?)"
_SEVERITY_RE = re.compile(
    # A pair counts only next to a risk or severity word ("CRITICAL/HIGH risks",
    # "severity CRITICAL or HIGH"), so "HIGH/LOW watermark" is not a label.
    rf"\b(?:{_SEV})\s*(?:/|,|\bor\b|\band\b)\s*(?:{_SEV})\b"
    rf"(?=[\s-]+(?:\w+\s+){{0,2}}?(?i:{_RISK_WORD})\b)"
    rf"|(?<=(?i:severity) )(?:{_SEV})\s*(?:/|,|\bor\b|\band\b)\s*(?:{_SEV})\b"
    rf"|\b(?:{_SEV})\b(?=[\s-]+(?i:{_RISK_WORD})\b)"
    rf"|(?<=(?i:severity) )(?:{_SEV})\b"
)


@dataclass(frozen=True)
class StyleHit:
    kind: str  # "dash" | "filler" | "hedge" | "construction" | "severity_caps"
    term: str
    excerpt: str


def _excerpt(text: str, start: int, end: int, radius: int = 40) -> str:
    return text[max(0, start - radius) : end + radius].replace("\n", " ")


def find_style_hits(text: str, identifiers: Iterable[str] = ()) -> list[StyleHit]:
    """Every item of the style list found in the prose of ``text``.

    ``identifiers`` (optional): names that are data, not words (the collected
    table and column names), skipped wherever they appear.
    """
    if not text:
        return []
    spans = _protected_spans(text, identifiers)
    prose = _blank(text, spans)
    hits: list[StyleHit] = []
    for start, end in _em_dashes(text, spans):
        term = "--" if "--" in text[start:end] else "—"
        hits.append(StyleHit("dash", term, _excerpt(text, start, end)))
    for m in _FILLER_RE.finditer(prose):
        hits.append(StyleHit("filler", m.group(0).lower(), _excerpt(text, m.start(), m.end())))
    for m in _HEDGE_RE.finditer(prose):
        term = " ".join(m.group(0).lower().split())
        hits.append(StyleHit("hedge", term, _excerpt(text, m.start(), m.end())))
    for m in _BOUNDED_HEDGE_RE.finditer(prose):
        if not _states_a_bound(text, m.start(), m.end()):
            term = " ".join(m.group(0).lower().split())
            hits.append(StyleHit("hedge", term, _excerpt(text, m.start(), m.end())))
    for label, rx in _CONSTRUCTION_RES:
        for m in rx.finditer(prose):
            hits.append(StyleHit("construction", label, _excerpt(text, m.start(), m.end())))
    code = _protected_spans(text, code_only=True)
    for m in _SEVERITY_RE.finditer(text):
        if not _inside(m.start(), code):
            hits.append(StyleHit("severity_caps", m.group(0), _excerpt(text, m.start(), m.end())))
    return hits


@dataclass(frozen=True)
class CleanResult:
    text: str  # the text to keep: the cleaned model text, or the fallback
    accepted: bool  # False when the model text was rejected
    reason: str  # why it was rejected (for the trace only), "" when accepted


def clean_model_prose(
    model_text: str, fallback: str, identifiers: Iterable[str] = ()
) -> CleanResult:
    """Clean one model-written customer field: replace dashes, then reject the
    field back to ``fallback`` (the deterministic text) if any other hit is left."""
    names = list(identifiers)
    cleaned = replace_dashes(model_text, names)
    hits = find_style_hits(cleaned, names)
    if not hits:
        return CleanResult(cleaned, True, "")
    terms = sorted({f"{h.kind}:{h.term}" for h in hits})
    return CleanResult(fallback, False, "plain-prose style list: " + ", ".join(terms))


def collector_identifiers(collector: dict[str, Any] | None) -> set[str]:
    """The collected table, view and column names: data, never words."""
    schema = (collector or {}).get("database_schema") or {}
    names: set[str] = set()
    for key in ("tables", "views"):
        for t in schema.get(key) or []:
            if not isinstance(t, dict):
                continue
            for field in ("table_id", "table_name", "view_name", "name"):
                value = t.get(field)
                if isinstance(value, str) and value:
                    names.add(value)
                    names.add(value.rsplit(".", 1)[-1])
            for c in t.get("columns") or []:
                if isinstance(c, dict) and isinstance(c.get("column_name"), str):
                    names.add(c["column_name"])
    return names


def _base_words() -> list[str]:
    """The filler list without its inflections, for a prompt."""
    bases: list[str] = []
    for w in FILLER_WORDS:
        if not any(w != b and w.startswith(b.rstrip("e")) for b in bases):
            bases.append(w)
    return bases


def style_list_markdown() -> str:
    """The style list as Markdown bullets, word for word what the skills that
    write customer prose carry (``tests/unit/shared/test_prose_style.py`` checks
    each skill quotes it), so a model is told exactly what its seam rejects."""
    words = ", ".join(_base_words())
    hedges = ", ".join(f'"{h}"' for h in HEDGES)
    return "\n".join(
        [
            "- No em dash (the long dash) and no spaced double hyphen: use a comma, a "
            "colon, parentheses or a new sentence. An en dash for a range (9 am - 5 pm, "
            "30s-60s) is fine. Put commands, flags and code in backticks.",
            f"- None of these words: {words}.",
            f'- No hedges: {hedges} ("generally available" is fine). State the fact. '
            "A softener on an effect is fine only with its bound in the sentence "
            '("stale until the TTL expires", "up to about 1 s behind"), and "roughly" '
            'only before a figure ("roughly 40%").',
            '- None of these constructions: "not just ... but", "not only ... but", '
            '"it\'s worth noting", "here\'s", "let\'s".',
            '- Severity in plain lowercase ("critical or high risks"), never an '
            'all-caps label such as "CRITICAL/HIGH" inside a sentence.',
        ]
    )
