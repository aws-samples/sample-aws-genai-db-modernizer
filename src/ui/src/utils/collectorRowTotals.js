// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// #1636: a table's row count is a number, a real 0, or unknown (null): the source has no
// count for it. The monitoring page's "Total rows" sums the known counts only and says
// how many are unknown; with no known count at all the total is not known, never 0.

import { formatCompactCount } from './format';

const isKnown = (count) => Number.isInteger(count) && count >= 0;

export function collectorRowTotals(tables) {
  const rows = Array.isArray(tables) ? tables : [];
  const known = rows.filter(t => t && isKnown(t.row_count));
  return {
    totalRows: known.reduce((sum, t) => sum + t.row_count, 0),
    knownRowTables: known.length,
    unknownRowTables: rows.length - known.length,
  };
}

// The figure the page shows: "12.5K", "1.2M", "830", or null when no count is known
// (the page then shows its "Not known" label).
export function totalRowsText(totals) {
  if (!totals || !totals.knownRowTables) return null;
  return formatCompactCount(totals.totalRows);
}
