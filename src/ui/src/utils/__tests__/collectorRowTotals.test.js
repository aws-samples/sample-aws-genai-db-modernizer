/**
 * #1636: the monitoring page's "Total rows" never counts an unknown table as empty.
 */
import { collectorRowTotals, totalRowsText } from '../collectorRowTotals';
import enMessages from '../../locales/en.json';

describe('collectorRowTotals', () => {
  test('sums the known counts and counts the unknown ones', () => {
    const totals = collectorRowTotals([
      { row_count: 12000 },
      { row_count: 0 },
      { row_count: null },
      { row_count: 500 },
    ]);
    expect(totals).toEqual({ totalRows: 12500, knownRowTables: 3, unknownRowTables: 1 });
    expect(totalRowsText(totals)).toBe('12.5K');
  });

  test('is not known, never 0, when no count is known', () => {
    const totals = collectorRowTotals([{ row_count: null }, { row_count: null }]);
    expect(totals.unknownRowTables).toBe(2);
    expect(totalRowsText(totals)).toBeNull();
    expect(enMessages['job-summary.collector.total-rows-unknown']).toBe('Not known');
  });

  test('a real 0 is shown as 0', () => {
    expect(totalRowsText(collectorRowTotals([{ row_count: 0 }]))).toBe('0');
  });

  test('handles no tables', () => {
    expect(collectorRowTotals(undefined)).toEqual({ totalRows: 0, knownRowTables: 0, unknownRowTables: 0 });
    expect(totalRowsText(collectorRowTotals([]))).toBeNull();
  });
});
