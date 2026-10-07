/**
 * The current-cost baseline / savings figure as every deliverable shows it
 * (#334): `tco_analysis.current_monthly_cost` and `savings_percent` are
 * `0`/`0.0` both when the source genuinely has no baseline (the collector
 * reported no RDS instance metadata) and -- in principle -- when the
 * baseline really is zero, so a bare "$0.00"/"0%" reads as a cost comparison
 * that was attempted and came out even, not as "no comparison was possible".
 *
 * Since #380, `tco_analysis.current_cost_known` says which case it is.
 * `isCostBaselineUnknown` matches `src/report/renderers.py::_cost_baseline_text`
 * exactly (#334 review) -- the one rule every deliverable (the decision and
 * engineering reports there; this UI, its HTML export, and
 * `analysis_report.py::_interactive_cost_baseline_stats` here) shares, so
 * none of them can disagree on the same report: unknown when
 * `current_cost_known` is `false`, or it is missing/`null` and
 * `current_monthly_cost` is falsy (covers a report written before
 * `current_cost_known` existed whose `current_monthly_cost` is still a
 * literal `0` placeholder). A report with no `current_cost_known` field but a
 * real nonzero `current_monthly_cost` keeps showing its number.
 */

/** True when `tco.current_monthly_cost` cannot be trusted as a real baseline. */
export function isCostBaselineUnknown(tco) {
  if (!tco) return true;
  if (tco.current_cost_known === false) return true;
  if ((tco.current_cost_known === undefined || tco.current_cost_known === null) && !tco.current_monthly_cost) return true;
  return false;
}

/** "0%", "44%", "44.3%" -- trailing zeros dropped, mirrors renderers.fmt_num(x, 1). */
export function formatSavingsPercent(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return '0%';
  const rounded = Math.round(n * 10) / 10;
  return `${Number.isInteger(rounded) ? rounded.toFixed(0) : rounded.toFixed(1)}%`;
}

/** "$123.45/mo" for a known baseline. */
export function formatCurrentCostStat(value) {
  const n = Number(value);
  return `$${(Number.isFinite(n) ? n : 0).toFixed(2)}/mo`;
}

/**
 * `{ known, currentCostStat, savingsStat }` for `tco_analysis`: the formatted
 * current-cost/savings text when there is a real baseline, or `null`s (so the
 * caller supplies its own "unknown" wording -- translated in the React page,
 * a fixed English string in the i18n-exempt standalone HTML export) when
 * there is not.
 */
export function costBaselineStats(tco) {
  const known = !isCostBaselineUnknown(tco);
  return {
    known,
    currentCostStat: known ? formatCurrentCostStat(tco?.current_monthly_cost) : null,
    savingsStat: known ? formatSavingsPercent(tco?.savings_percent) : null,
  };
}
