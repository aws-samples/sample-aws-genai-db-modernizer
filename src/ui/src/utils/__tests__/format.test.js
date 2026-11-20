// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import fs from 'fs';
import path from 'path';
import { formatFigure as formatFigureFromFacts } from '../reportFacts';
import {
  formatBytes, formatCompactCount, formatCount, formatDatabaseSize, formatDateTime, formatDecimal, formatFigure,
  formatFigurePercent, formatFixed, formatHours, formatMilliseconds, formatMoney, formatMonthlyMoney, formatPercent,
  formatRate, formatReportDate, formatShortId, formatSignificant,
} from '../format';

describe('a missing figure and a negative amount (#2347)', () => {
  it.each([null, undefined, '', 'abc'])('formatFigure prints the missing marker for %p, never 0', (value) => {
    expect(formatFigure(value, 1)).toBe('—');
  });

  it('formatFigure keeps a real zero as 0', () => {
    expect(formatFigure(0, 1)).toBe('0');
  });

  it('formatMoney puts the sign after the dollar sign, as the reports do', () => {
    expect(formatMoney(-5)).toBe('$-5.00');
    expect(formatMoney(-1234.5)).toBe('$-1,234.50');
    expect(formatMoney(-5, { whole: true })).toBe('$-5');
    expect(formatMonthlyMoney(-5)).toBe('$-5.00/mo');
  });
});

describe('formatMoney (#2309)', () => {
  it('prints dollars with a thousands separator and two decimals', () => {
    expect(formatMoney(1178.33)).toBe('$1,178.33');
    expect(formatMoney(100.6)).toBe('$100.60');
    expect(formatMoney(0)).toBe('$0.00');
    expect(formatMoney(1234567.891)).toBe('$1,234,567.89');
  });

  it('prints whole dollars on request', () => {
    expect(formatMoney(1178.33, { whole: true })).toBe('$1,178');
  });

  it('reads a missing or non-numeric amount as a dash, never $NaN', () => {
    expect(formatMoney(null)).toBe('—');
    expect(formatMoney(undefined)).toBe('—');
    expect(formatMoney('abc')).toBe('—');
  });

  it('adds the monthly suffix once', () => {
    expect(formatMonthlyMoney(1178.33)).toBe('$1,178.33/mo');
  });
});

describe('formatRate (#2309)', () => {
  // A rate prints as the reports and the cost tiles print it (fmt_num, #2285),
  // so a query table and its "Sized for ... calls/s" line never disagree.
  it.each([185.07, 96.51, 0.0042, 12.345, 29795.38, 0, -0.001])('prints %p as formatFigure does', (value) => {
    expect(formatRate(value)).toBe(formatFigure(value));
  });

  it('pins the values the reports print', () => {
    expect(formatRate(185.07)).toBe('185.07');
    expect(formatRate(96.51)).toBe('96.51');
    expect(formatRate(0.0042)).toBe('<0.01');
    expect(formatRate(29795.38)).toBe('29,795.38');
  });

  it('reads a missing amount as a dash', () => {
    expect(formatRate(null)).toBe('—');
    expect(formatRate(undefined)).toBe('—');
    expect(formatRate('abc')).toBe('—');
  });
});

describe('formatPercent, formatCount, formatShortId (#2309)', () => {
  it('formats a percentage with a fixed number of decimals', () => {
    expect(formatPercent(44.256)).toBe('44.3%');
    expect(formatPercent(44.256, 0)).toBe('44%');
    expect(formatPercent(null)).toBe('—');
  });

  it('formats a count with separators', () => {
    expect(formatCount(1234567)).toBe('1,234,567');
    expect(formatCount(null)).toBe('—');
  });

  it('shortens an id to eight characters', () => {
    expect(formatShortId('b4f93edf-1111-2222-3333-444444444444')).toBe('b4f93edf');
    expect(formatShortId('abc')).toBe('abc');
    expect(formatShortId(null)).toBe('—');
  });
});

// The rule the issue asks for: new figures go through format.js. The guard is
// noHandFormatting.test.js (#2347), which replaced the per-file toFixed ratchet.

describe('formatFigure, the twin of fmt_num (#2347)', () => {
  it.each([
    [17.460499999999996, 2, '17.46'],
    [3.0, 2, '3'],
    [29795.38, 2, '29,795.38'],
    [0.0042, 2, '<0.01'],
    [-0.0042, 2, '>-0.01'],
    [-0.0, 2, '0'],
    [44.25, 1, '44.3'],
    [44.0, 1, '44'],
    [0.04, 1, '<0.1'],
    [-0.04, 1, '>-0.1'],
    [1234.5, 1, '1,234.5'],
    [75, 0, '75'],
    [0.3, 0, '<1'],
  ])('fmt_num(%p, %p) is %p', (value, decimals, text) => {
    expect(formatFigure(value, decimals)).toBe(text);
  });

  it('is the function reportFacts.js exports', () => {
    expect(formatFigureFromFacts).toBe(formatFigure);
  });
});

describe('formatFigurePercent (#2347)', () => {
  it('prints fmt_num and a percent sign, as the reports do', () => {
    expect(formatFigurePercent(19.0, 1)).toBe('19%');
    expect(formatFigurePercent(19.34, 1)).toBe('19.3%');
    expect(formatFigurePercent(0.04, 1)).toBe('<0.1%');
    expect(formatFigurePercent(12.345)).toBe('12.35%');
    expect(formatFigurePercent(null)).toBe('—');
    expect(formatFigurePercent('abc')).toBe('—');
  });
});

describe('formatFixed and formatDecimal (#2347)', () => {
  it('formatFixed prints a fixed number of places with no separators, as Python ":.Nf"', () => {
    expect(formatFixed(41.25, 1)).toBe('41.3');
    expect(formatFixed(1234.5, 1)).toBe('1234.5');
    expect(formatFixed(75.3, 2)).toBe('75.30');
    expect(formatFixed(12345.6, 0)).toBe('12346');
    expect(formatFixed(null, 1)).toBe('—');
  });

  it('formatDecimal prints a fixed number of places with separators', () => {
    expect(formatDecimal(2, 1)).toBe('2.0');
    expect(formatDecimal(1234.56, 1)).toBe('1,234.6');
    expect(formatDecimal(undefined, 1)).toBe('—');
  });

  it('formatMilliseconds adds the unit', () => {
    expect(formatMilliseconds(12.34)).toBe('12.3ms');
    expect(formatMilliseconds(null)).toBe('—');
  });

  it('formatSignificant keeps three significant digits by default', () => {
    expect(formatSignificant(185.0712)).toBe('185');
    expect(formatSignificant(0.004219)).toBe('0.00422');
    expect(formatSignificant(29795.38)).toBe('29,800');
    expect(formatSignificant(null)).toBe('—');
  });
});

describe('sizes and compact counts (#2347)', () => {
  it('formatBytes prints B, KB, MB and GB', () => {
    expect(formatBytes(512)).toBe('512 B');
    expect(formatBytes(20 * 1024)).toBe('20 KB');
    expect(formatBytes(1.24 * 1024 * 1024)).toBe('1.2 MB');
    expect(formatBytes(3 * 1024 * 1024 * 1024)).toBe('3 GB');
    expect(formatBytes(null)).toBe('');
  });

  it('formatDatabaseSize prints GB from one gigabyte up, MB below', () => {
    expect(formatDatabaseSize(12.34)).toBe('12.3 GB');
    expect(formatDatabaseSize(1)).toBe('1.0 GB');
    expect(formatDatabaseSize(0.5)).toBe('512 MB');
    expect(formatDatabaseSize(0)).toBe('—');
    expect(formatDatabaseSize(null)).toBe('—');
  });

  it('formatCompactCount prints K and M above a thousand and a million', () => {
    expect(formatCompactCount(830)).toBe('830');
    expect(formatCompactCount(1000)).toBe('1,000');
    expect(formatCompactCount(12500)).toBe('12.5K');
    expect(formatCompactCount(1234567)).toBe('1.2M');
    expect(formatCompactCount(null)).toBe('—');
  });
});

describe('formatDateTime (#2347)', () => {
  it('prints a date and time in en-US, and null for none', () => {
    const text = formatDateTime('2026-11-18T10:20:30Z');
    expect(text).toMatch(/^Nov 18, 2026, \d{2}:\d{2}:\d{2} (AM|PM)$/);
    expect(formatDateTime(null)).toBeNull();
    expect(formatDateTime('')).toBeNull();
  });
});

describe('the audited spots use the formatter (#2309)', () => {
  const root = path.resolve(__dirname, '..', '..');
  const read = (rel) => fs.readFileSync(`${root}/${rel}`, 'utf8');

  it('the executive tiles and cost breakdown print money through formatMoney', () => {
    expect(read('components/ExecutiveSummaryStats.js')).toMatch(/formatMonthlyMoney\(projectedCost\)/);
    expect(read('components/ExecutiveSummaryStats.js')).toMatch(/formatMoney\(row\.monthly_cost_usd\)/);
    expect(read('components/CostBreakdown.js')).toMatch(/formatMoney\(item\.cost\)/);
  });

  it('the query journey prints rates through formatRate and the engine by its display name', () => {
    const panel = read('components/QueryDetailPanel.js');
    expect(panel).toMatch(/formatRate\(source\.frequency_per_hour\)/);
    expect(panel).toMatch(/formatRate\(source\.calls_per_second\)/);
    expect(panel).toMatch(/<EngineBadge engine=\{assignment\.assigned_engine\}/);
  });
});

describe('formatHours (#2306)', () => {
  it('prints one decimal and an h, and a dash for an absent value', () => {
    expect(formatHours(1.5)).toBe('1.5h');
    expect(formatHours(0)).toBe('0.0h');
    expect(formatHours(null)).toBe('—');
    expect(formatHours(undefined)).toBe('—');
  });
});

describe('the dashboard prints its figures through the shared formatter (#2306)', () => {
  it('Dashboard.js formats counts, percent and hours with format.js', () => {
    const page = fs.readFileSync(path.join(__dirname, '../../pages/Dashboard.js'), 'utf8');
    expect(page).toMatch(/formatCount\(s\.total_assessments\)/);
    expect(page).toMatch(/formatPercent\(s\.success_rate_percent\)/);
    expect(page).toMatch(/formatHours\(s\.average_duration_hours\)/);
    expect(page).not.toMatch(/\.toFixed\(/);
  });
});

describe('formatReportDate (#2314)', () => {
  // The results page header states the date the report was written, the same
  // calendar day the executive report's cover prints ("Generated 2026-11-18"):
  // the timestamp's own date, never shifted by the reader's time zone.
  it('prints the date part of the report timestamp', () => {
    expect(formatReportDate('2026-11-18T15:10:47.174567Z')).toBe('Nov 18, 2026');
    expect(formatReportDate('2026-11-14')).toBe('Nov 14, 2026');
  });

  it('keeps the stated day late in the UTC evening', () => {
    expect(formatReportDate('2026-11-18T23:59:59Z')).toBe('Nov 18, 2026');
    expect(formatReportDate('2026-01-01T00:00:00Z')).toBe('Jan 1, 2026');
  });

  // A formatter that reads the timestamp as an instant (`new Date(ts)`) and prints it in
  // UTC gets these wrong even when CI runs with TZ=UTC: the offset moves the instant onto
  // the next or the previous UTC day. The cover prints the first ten characters.
  it('keeps the stated day of a timestamp with an offset, whatever the reader time zone', () => {
    expect(formatReportDate('2026-11-18T23:30:00-05:00')).toBe('Nov 18, 2026');
    expect(formatReportDate('2026-11-19T00:30:00+09:00')).toBe('Nov 19, 2026');
    expect(formatReportDate('2026-01-01T00:15:00+14:00')).toBe('Jan 1, 2026');
  });

  it('prints the same day as the first ten characters the executive report cover prints', () => {
    const stamps = [
      '2026-11-18T23:30:00-05:00',
      '2026-11-19T00:30:00+09:00',
      '2026-12-31T23:59:59-11:00',
      '2026-11-18T15:10:47.174567Z',
    ];
    stamps.forEach((ts) => {
      const [y, m, d] = ts.slice(0, 10).split('-').map(Number);
      const month = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][m - 1];
      expect(formatReportDate(ts)).toBe(`${month} ${d}, ${y}`);
    });
  });

  it('prints nothing for a missing or unreadable timestamp, as the cover does', () => {
    expect(formatReportDate(undefined)).toBeNull();
    expect(formatReportDate(null)).toBeNull();
    expect(formatReportDate('')).toBeNull();
    expect(formatReportDate('yesterday')).toBeNull();
    expect(formatReportDate('2026-13-45T00:00:00Z')).toBeNull();
    expect(formatReportDate(1763478647)).toBeNull();
  });
});
