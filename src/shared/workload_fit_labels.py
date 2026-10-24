"""Customer-facing label and definition for the routing fit score (#152, bug: Deliverables
present a fit score as a confidence percentage).

Every deliverable shows a per-engine and per-query score computed by
``src.shared.ranking`` (``engine_confidence``, ``confidence_text``) and carried on
contracts as ``confidence`` / ``routed_confidence``. Those are internal field names
and stay unchanged -- this module only governs the word a reader sees next to the
number. It used to be "confidence", which reads as "we are 78% sure this will
work". It is actually a fit score: how closely the engine's capabilities match the
access patterns of the queries routed to it. The deck already hedged with "not a
forecast"; this module makes the hedge the label itself.

Single source of truth for both halves of that fix:

- ``WORKLOAD_FIT_LABEL``: the short label a column header, slide title or filter
  property uses ("Workload fit", "Engine fit").
- ``WORKLOAD_FIT_WORD``: the word a sentence appends to a percentage
  ("78% fit" instead of "78% confidence").
- ``WORKLOAD_FIT_DEFINITION``: the one plain-language sentence every deliverable
  prints once, at the figure's first appearance.

Mirrored by hand in ``src/ui/src/utils/workloadFitLabels.js`` (imported by
the live React results page) the same
way ``src/shared/engine_names.py`` is mirrored by ``engineNames.js`` --
``tests/unit/report/test_workload_fit_labels_js_sync.py`` parses that file and
fails if the two drift.
"""

from __future__ import annotations

from typing import Literal

#: Why a query is pinned to its engine rather than placed by its score
#: (``QueryAssignment.pinned``).
PinKind = Literal["utility"]

#: The short label for a column header, slide title or filter property.
WORKLOAD_FIT_LABEL = "Workload fit"

#: The word a sentence appends to a percentage: "78% fit", not "78% confidence".
WORKLOAD_FIT_WORD = "fit"

#: The one plain-language sentence every deliverable prints once, at the figure's
#: first appearance, so "fit" can never be mistaken for a success probability.
WORKLOAD_FIT_DEFINITION = (
    "Workload fit: how closely this engine's capabilities match the access "
    "patterns of the queries routed to it. It is not a probability that the "
    "migration will succeed."
)

#: The short label for the cache layer's figure (#1291). The cache layer owns no
#: query, so its figure is never shown under ``WORKLOAD_FIT_LABEL``.
CACHE_FIT_LABEL = "Cache fit"

#: Stated once, right after ``WORKLOAD_FIT_DEFINITION``, in every deliverable that
#: lists a cache layer (#1291): what the cache layer's figure measures.
CACHE_FIT_DEFINITION = (
    "Cache fit: how closely the cache layer's capabilities match the "
    "reads it fronts. The cache layer owns no queries, so this is not a workload "
    "fit and is not compared with the owning engines' figures."
)

#: ``QueryAssignment.pinned`` for a utility or DDL statement the first assignment pins
#: to the relational engine (Move rule ``override.utility``, #1373).
PINNED_UTILITY: PinKind = "utility"

#: What every view shows instead of a pinned query's percentage (maintainer decision on
#: #1381, 2026-10-22): the pin, not its score, placed the query. The UI shows the same
#: wording through its ``common.fit.pinned-utility`` i18n key
#: (``tests/unit/agents/referee/test_utility_pin_label.py::test_the_label_is_one_wording_in_python_and_the_ui``
#: keeps the two equal).
PINNED_LABELS: dict[str, str] = {PINNED_UTILITY: "pinned (utility)"}
PINNED_UTILITY_LABEL = PINNED_LABELS[PINNED_UTILITY]


def pinned_label(pinned: str | None) -> str | None:
    """The label a pinned query shows instead of its fit, ``None`` for a scored query."""
    return PINNED_LABELS.get(pinned) if pinned else None


def query_fit_text(confidence: float | int | None, pinned: str | None) -> str:
    """A query's fit as every view shows it: its pin's label, else ``78%`` (``-`` with
    no figure). Formats only; ``confidence`` and ``pinned`` come from the assignment."""
    label = pinned_label(pinned)
    if label:
        return label
    return "-" if confidence is None else f"{round(confidence)}%"
