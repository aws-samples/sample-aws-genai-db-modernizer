/**
 * Why the generated synthesis executive summary was withheld, in one clause
 * (#393, ported for #269).
 *
 * Mirrors ``_FALLBACK_REASONS`` / ``fallback_withheld_reason`` in
 * src/report/renderers.py. Three independent high-confidence checks can
 * reject the generated summary (``apply_synthesis_llm_output``:
 * ``check_summary_grounding``, ``check_summary_internal_leaks``,
 * ``check_summary_wave_order``), each stamping its own "[high confidence] ..."
 * message onto `summary_validation_warnings` with its own wording -- read here
 * instead of hard-coding the table-attribution reason, which used to show even
 * when a different check was the one that actually rejected it.
 *
 * Reality-check summaries (#236) have only one check, so this reason mapping
 * does not apply there -- AssignmentGate.js shows its raw warnings instead.
 */
// Exported (not just module-local) so fallbackWithheldReason.parity.test.js
// can assert this table still matches renderers.py's own, marker for marker.
export const FALLBACK_REASONS = [
  ['internal field', "named one of this codebase's internal field names"],
  ['migration waves move', 'stated a migration order that contradicts the roadmap'],
  [
    'no in-scope query of that engine touches it',
    'named a table under an engine that does not serve it',
  ],
];

/**
 * `warnings` is the synthesis summary's `summary_validation_warnings` array.
 * Falls back to the original, table-attribution wording when none of its
 * `[high confidence]` entries carry a known marker (e.g. an older
 * `report.json` written before a given check existed) -- a missing or
 * unrecognised warning should read as the historical default, not an empty
 * clause.
 */
export function fallbackWithheldReason(warnings) {
  const list = Array.isArray(warnings) ? warnings.filter((w) => typeof w === 'string') : [];
  const high = list.filter((w) => w.startsWith('[high confidence]'));
  const reasons = FALLBACK_REASONS
    .filter(([marker]) => high.some((w) => w.includes(marker)))
    .map(([, text]) => text);
  if (reasons.length === 0) return FALLBACK_REASONS[FALLBACK_REASONS.length - 1][1];
  if (reasons.length === 1) return reasons[0];
  return `${reasons.slice(0, -1).join(', ')} and ${reasons[reasons.length - 1]}`;
}
