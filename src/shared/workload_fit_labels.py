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
``ExportReport.js``'s standalone export and the live React results page) the same
way ``src/shared/engine_names.py`` is mirrored by ``engineNames.js`` --
``tests/unit/report/test_workload_fit_labels_js_sync.py`` parses that file and
fails if the two drift.
"""

from __future__ import annotations

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


def fit_phrase(pct_text: str) -> str:
    """``93%`` -> ``93% fit``; ``60% (signal only)`` -> ``60% fit (signal only)``.

    Mirrors the old ``pptx_report._confidence_phrase`` shape: the percentage (and
    any trailing evidence note in parentheses) is unchanged, only the trailing
    word changes.
    """
    pct, _, note = pct_text.partition(" (")
    return f"{pct} {WORKLOAD_FIT_WORD}" + (f" ({note}" if note else "")
