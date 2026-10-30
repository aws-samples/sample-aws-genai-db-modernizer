// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Display names for Aurora Optimization.category values, shown by the results
 * page's "Aurora Design" section (#587 round 3: the raw category code read
 * "read_replica: Aurora cluster", "io_optimized: Aurora MySQL cluster",
 * instead of a readable label; the retired HTML report had the fix, and the
 * results page took it over in #987).
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
