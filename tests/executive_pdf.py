"""Shared helpers for the executive report PDF tests (#1020): its text, the table
names it must never show, and where every run of text is drawn."""

from __future__ import annotations

import io
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pypdf import PdfReader
from reportlab.pdfbase import pdfmetrics

from src.report import executive_report


def normalize(text: str) -> str:
    """Collapse whitespace runs: the PDF text layer breaks lines mid-sentence."""
    return " ".join(text.split())


def _reader(data: bytes | Path) -> PdfReader:
    return PdfReader(io.BytesIO(data) if isinstance(data, bytes) else str(data))


def page_texts(data: bytes | Path) -> list[str]:
    return [normalize(p.extract_text() or "") for p in _reader(data).pages]


def text(data: bytes | Path) -> str:
    """The whole document's text, the page furniture (footer, page numbers and the
    "(continued)" titles) left in."""
    return " ".join(page_texts(data))


def body_text(data: bytes | Path, db: str) -> str:
    """The document's text without the page furniture, so a card or table that
    continues on the next page reads as one."""
    furniture = [
        normalize(f"{executive_report.PRODUCT_NAME} · {db}"),
        normalize(executive_report.FOOTER),
        *(f"{h}{executive_report.CONTINUED}" for h in executive_report.SECTION_HEADINGS),
    ]
    out = []
    for page in page_texts(data):
        for f in furniture:
            page = page.replace(f, " ")
        out.append(re.sub(r"Page \d+ of \d+", " ", page))
    return normalize(" ".join(out))


def outline(data: bytes | Path) -> list[str]:
    return [o.title for o in _reader(data).outline if not isinstance(o, list)]


def printed(value: Any) -> str:
    """How a facts-layer string reads back from the PDF (the font has no arrow)."""
    return normalize(executive_report.printable(value))


def collected_table_names(collector_output: dict[str, Any]) -> set[str]:
    """Every name the collector found: tables, views, procedures and functions,
    triggers and columns, by id, bare and schema-qualified name."""
    schema = collector_output.get("database_schema") or {}
    names: set[str] = set()
    for key, kind in (
        ("tables", "table"),
        ("views", "view"),
        ("procedures", "procedure"),
        ("triggers", "trigger"),
    ):
        for obj in schema.get(key) or []:
            names |= {str(obj.get(f"{kind}_id") or ""), str(obj.get(f"{kind}_name") or "")}
            if obj.get("schema_name"):
                names.add(f"{obj['schema_name']}.{obj.get(f'{kind}_name')}")
            names |= {str(c.get("column_name") or "") for c in obj.get("columns") or []}
    return {n for n in names if len(n) > 1}


_DISTINCTIVE = re.compile(r"[_\d$.]|[a-z][A-Z]")
_SNAKE = re.compile(r"(?<![\w$])[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]*[A-Za-z0-9](?![\w$])")


def table_name_hits(content: str, names: set[str]) -> list[str]:
    """Each identifier in ``content``, with its context. A distinctive name (an
    underscore, a digit, a dot or an inner capital) counts anywhere, in any case,
    with or without a schema prefix; any snake_case word counts; a plain word
    (``users``, ``Customer``, ``each``) counts where it reads as an identifier:
    quoted, bracketed, schema-qualified, after FROM/JOIN/INTO/UPDATE, or after a
    kind label ("table: users"). As a bare word it is prose ("the customer's own
    choice")."""
    hits = []

    def hit(name: str, m: re.Match[str] | None) -> None:
        if m:
            hits.append(f"{name}: ...{content[max(0, m.start() - 60) : m.end() + 20]}...")

    for name in sorted(names):
        n = re.escape(name)
        if _DISTINCTIVE.search(name):
            hit(name, re.search(r"(?<![\w$])" + n + r"(?![\w$])", content, re.I))
        else:
            hit(
                name,
                re.search(
                    rf"[`\"\[]\s*{n}\s*[`\"\]]|[\w$]+\.{n}(?![\w$])|"
                    rf"\b(?-i:FROM|JOIN|INTO|UPDATE)\s+{n}(?![\w$])|"
                    rf"\b(?:table|view|function|procedure|trigger|column)\s*:\s*{n}(?![\w$])",
                    content,
                    re.I,
                ),
            )
    for m in _SNAKE.finditer(content):
        hit(m.group(0), m)
    return hits


_SQL_FRAGMENTS = (
    re.compile(r"`|\[\w[^\]]*\]"),
    re.compile(r"\bselect\b[^.;]*?\bfrom\b", re.I),
    re.compile(r"\b(?:insert\s+into|delete\s+from)\b|\bupdate\s+\S+\s+set\b", re.I),
    re.compile(r"\bis\s+(?:not\s+)?null\b|\b\w+\s*\(\s*\*\s*\)", re.I),
    # "where x is" only as a null test: the unservable intro's "keep each query where
    # it is" is English (found by #1148's flagged-query fixture).
    re.compile(
        r"\b(?:group|order)\s+by\b"
        r"|\bwhere\s+\S+\s*(?:=|<|>|\bin\b|\blike\b|\bis\s+(?:not\s+)?null\b)",
        re.I,
    ),
    re.compile(r"[\w.$]+\s*(?:=|<>|!=|<=|>=)\s*(?:\?|\$\d+|'[^']*'|\d+(?![\w.]))"),
    re.compile(r"\b(?:SELECT|FROM|WHERE|JOIN|HAVING)\b"),
)


def sql_hits(content: str) -> list[str]:
    """Each SQL fragment in ``content``: quoting, a statement's shape in any case, a
    predicate (``col = ?``, ``IS NULL``), a starred call (``COUNT(*)``), a grouping,
    a keyword in capitals."""
    out = []
    for rx in _SQL_FRAGMENTS:
        for m in rx.finditer(content):
            out.append(content[max(0, m.start() - 40) : m.end() + 20])
    return out


@dataclass(frozen=True)
class TextRun:
    page: int
    text: str
    x: float
    y: float
    width: float
    size: float


def text_runs(data: bytes | Path) -> list[TextRun]:
    """Every run of text drawn, with its position and measured width (points)."""
    executive_report.register_fonts()
    runs: list[TextRun] = []
    for n, page in enumerate(_reader(data).pages, start=1):

        def visit(t: str, cm: Any, tm: Any, font: Any, size: float, n: int = n) -> None:
            if not t.strip() or font is None:
                return
            x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
            y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
            scale = (tm[0] or 1.0) * (cm[0] or 1.0)
            base = str(font.get("/BaseFont", ""))
            face = executive_report.font_for("Bd" in base or "Bold" in base)
            pts = size * scale
            width = pdfmetrics.stringWidth(t.rstrip("\n"), face, pts)
            runs.append(TextRun(n, t, x, y, width, pts))

        page.extract_text(visitor_text=visit)
    return runs


def outside_the_page(data: bytes | Path, margin: float = 24.0) -> list[str]:
    """Runs of text that start or end outside the printable area of their page."""
    w, h = executive_report.PAGE_W, executive_report.PAGE_H
    bad = []
    for r in text_runs(data):
        if r.x < margin - 0.5 or r.x + r.width > w - margin + 0.5 or not (0 < r.y < h):
            bad.append(f"p{r.page} x={r.x:.1f} w={r.width:.1f} y={r.y:.1f}: {r.text[:60]!r}")
    return bad


def executive_text(report: dict[str, Any]) -> str:
    """Render ``report`` to the executive report and return its text, furniture removed."""
    data = executive_report.render_executive_report_pdf(report)
    return body_text(data, str(report.get("database_name") or "?"))


def engineering_text(report: dict[str, Any], **kwargs: Any) -> str:
    """The engineering report as a reader sees it: no Markdown escapes or emphasis,
    whitespace collapsed."""
    from src.report.engineering_report import render_engineering_report_md

    md = render_engineering_report_md(report, **kwargs)
    md = re.sub(r"\\([\\`*_{}\[\]()#+\-.!|<>~])", r"\1", md)
    return normalize(md.replace("**", "").replace("*", ""))
