/**
 * Customer-facing label and definition for the routing fit score (#152, bug:
 * Deliverables present a fit score as a confidence percentage), imported by
 * the live React results page.
 *
 * The underlying score (`routed_confidence`, `engine_confidence`, `confidence`,
 * `confidence_score`) keeps its internal field names -- only the word a reader
 * sees next to the number changes, from "confidence" (reads as "we are 78% sure
 * this will work") to "fit" (how closely the engine's capabilities match the
 * access patterns of the queries routed to it).
 *
 * Kept in sync with `src/shared/workload_fit_labels.py` by hand --
 * `tests/unit/report/test_workload_fit_labels_js_sync.py` parses this file and
 * fails if the two drift, the same way `engineNames.js` is pinned to
 * `src/shared/engine_names.py` by `test_engine_names_js_sync.py`.
 */
export const WORKLOAD_FIT_LABEL = 'Workload fit';

export const WORKLOAD_FIT_WORD = 'fit';

export const WORKLOAD_FIT_DEFINITION =
  'Workload fit: how closely this engine\'s capabilities match the access ' +
  'patterns of the queries routed to it. It is not a probability that the ' +
  'migration will succeed.';

/** `93%` -> `93% fit`; `60% (signal only)` -> `60% fit (signal only)`. */
export function fitPhrase(pctText) {
  const [pct, ...rest] = pctText.split(' (');
  const note = rest.join(' (');
  return `${pct} ${WORKLOAD_FIT_WORD}` + (note ? ` (${note}` : '');
}

/** The cache layer's figure is never shown under the workload fit label (#1291). */
export const CACHE_FIT_LABEL = 'Cache fit';

export const CACHE_FIT_DEFINITION =
  'Cache fit: how closely the cache layer\'s capabilities match the ' +
  'reads it fronts. The cache layer owns no queries, so this is not a workload ' +
  'fit and is not compared with the owning engines\' figures.';
