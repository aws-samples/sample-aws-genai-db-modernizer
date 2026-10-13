/**
 * Display names for Aurora Optimization.category values, imported by
 * ExportReport.js's standalone export (#587 round 3: the interactive
 * analysis report's "Aurora Design" section printed the raw category code
 * directly, e.g. "read_replica: Aurora cluster", "io_optimized: Aurora
 * MySQL cluster", instead of a readable label).
 *
 * Kept in sync with `src/shared/optimization_labels.py`'s
 * `OPTIMIZATION_DISPLAY_NAMES` by hand -- `tests/unit/report/
 * test_optimization_labels_js_sync.py` parses this file and fails if the
 * two drift, the same way `engineNames.js` is pinned to
 * `src/shared/engine_names.py` by `test_engine_names_js_sync.py`.
 */
export const OPTIMIZATION_LABELS = {
  partitioning: 'Partitioning',
  index: 'Index',
  read_replica: 'Read replica',
  io_optimized: 'I/O-Optimized storage',
  other: 'Other',
};

export const optimizationLabel = (category) => OPTIMIZATION_LABELS[category] || category;
