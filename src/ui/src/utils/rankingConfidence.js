/**
 * Engine confidence as the deliverables show it (#152).
 *
 * A synthesis ranking entry carries `analysis_confidence` (also `confidence_score`):
 * suitability averaged over every table the engine analyzed, kept for audit. It
 * also carries `routed_confidence`: the mean fit of the queries the assignment
 * routes to the engine (for the cache layer, of the reads it fronts). The UI shows
 * the routed figure; a report written before it existed, or an engine with no
 * routed query, falls back to `confidence_score`.
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

const defaultT = (key, { defaultValue, ...vars } = {}) => {
  let text = defaultValue !== undefined ? defaultValue : key;
  Object.entries(vars).forEach(([name, value]) => { text = text.split(`{{${name}}}`).join(value); });
  return text;
};

/**
 * The Target Database Details alert for one ranking entry (#152): the routed fit
 * with the queries it covers, or the legacy wording on the analysis average.
 */
export function confidenceAlertText(item, t = defaultT) {
  if (hasRoutedConfidence(item)) {
    return t('report-results.target-db-mapping.routed-confidence-alert', {
      score: engineConfidence(item),
      count: item.routed_queries ?? 0,
      defaultValue: 'The queries routed to this database fit it at {{score}}% on average ({{count}} queries)',
    });
  }
  return t('report-results.target-db-mapping.confidence-alert', {
    score: engineConfidence(item),
    defaultValue: 'This database is recommended with {{score}}% confidence based on workload analysis',
  });
}
