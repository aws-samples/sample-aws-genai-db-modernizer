/**
 * Engine confidence as the deliverables show it (#152, #312).
 *
 * A synthesis ranking entry carries `analysis_confidence` (also `confidence_score`):
 * suitability averaged over every table the engine analyzed, kept for audit. It
 * also carries `routed_confidence`: the mean fit of the queries the assignment
 * routes to the engine (for the cache layer, of the reads it fronts). The UI shows
 * the routed figure; a report written before it existed, or an engine with no
 * routed query, falls back to `confidence_score`.
 *
 * A routed confidence also carries `routed_confidence_evidence` (`table` /
 * `partial` / `signal_only`): whether a source table the engine's analysis rated
 * backs the fit. A `signal_only` fit is the basic baseline plus the signal bonus,
 * never a measurement, so it is always labelled "signal only — no table-level
 * evidence" (the deck and the decision/engineering reports use the same words,
 * see `src/shared/ranking.py`); a `partial` fit is labelled "partly signal-based"
 * only once at least a quarter of the engine's routed queries lack table evidence.
 */

const isNumber = (value) => typeof value === 'number' && Number.isFinite(value);

/** True when the entry has a routed confidence. */
export const hasRoutedConfidence = (item) => isNumber(item?.routed_confidence);

/** The confidence to show: routed, else the analysis average, else 0. */
export function engineConfidence(item) {
  if (hasRoutedConfidence(item)) return item.routed_confidence;
  return isNumber(item?.confidence_score) ? item.confidence_score : 0;
}

/** The audit figure (average over every analyzed table), or null. */
export function analysisConfidence(item) {
  const value = item?.analysis_confidence ?? item?.confidence_score;
  return isNumber(value) ? value : null;
}

// A routed confidence is "partly signal-based" when at least this share of the
// engine's routed queries has no table-level evidence (mirrors
// src/shared/ranking.py PARTIAL_EVIDENCE_MIN_SHARE, #152/#312).
export const PARTIAL_EVIDENCE_MIN_SHARE = 0.25;

export const SIGNAL_ONLY_NOTE = 'signal only — no table-level evidence';
export const SIGNAL_ONLY_SHORT = 'signal only';
export const PARTIAL_NOTE = 'partly signal-based';

/** True when no source table the engine's analysis rated backs its routed fit. */
export function isSignalOnly(item) {
  return hasRoutedConfidence(item) && item?.routed_confidence_evidence === 'signal_only';
}

/**
 * How much a routed confidence rests on signals alone, or '' when it does not
 * (mirrors `src.shared.ranking.evidence_note`, #312).
 */
export function evidenceNote(item, short = false) {
  if (!hasRoutedConfidence(item)) return '';
  const evidence = item?.routed_confidence_evidence;
  if (evidence === 'signal_only') return short ? SIGNAL_ONLY_SHORT : SIGNAL_ONLY_NOTE;
  if (evidence === 'partial') {
    const n = item?.routed_queries ?? 0;
    const unbacked = item?.routed_queries_without_table_evidence ?? 0;
    if (n && unbacked / n >= PARTIAL_EVIDENCE_MIN_SHARE) return PARTIAL_NOTE;
  }
  return '';
}

/** ``60% (signal only — no table-level evidence)``; ``93%`` when table-backed. */
export function confidenceText(item, short = false) {
  const note = evidenceNote(item, short);
  return `${engineConfidence(item)}%${note ? ` (${note})` : ''}`;
}

const defaultT = (key, { defaultValue, ...vars } = {}) => {
  let text = defaultValue !== undefined ? defaultValue : key;
  Object.entries(vars).forEach(([name, value]) => { text = text.split(`{{${name}}}`).join(value); });
  return text;
};

/**
 * The Target Database Details alert for one ranking entry (#152): the routed fit
 * with the queries it covers, or the legacy wording on the analysis average. A
 * signal-only or partial fit adds the same caveat the other deliverables show,
 * inside the query-count parenthetical (#312 review).
 */
export function confidenceAlertText(item, t = defaultT) {
  if (hasRoutedConfidence(item)) {
    const note = evidenceNote(item);
    const count = item.routed_queries ?? 0;
    return t('report-results.target-db-mapping.routed-confidence-alert', {
      score: engineConfidence(item),
      count,
      noteClause: note ? `; ${note}` : '',
      defaultValue: count === 1
        ? `The queries routed to this database fit it at {{score}}% on average ({{count}} query${note ? `; ${note}` : ''})`
        : `The queries routed to this database fit it at {{score}}% on average ({{count}} queries${note ? `; ${note}` : ''})`,
    });
  }
  return t('report-results.target-db-mapping.confidence-alert', {
    score: engineConfidence(item),
    defaultValue: 'This database is recommended with {{score}}% confidence based on workload analysis',
  });
}
