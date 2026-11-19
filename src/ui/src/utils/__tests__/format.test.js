// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import fs from 'fs';
import path from 'path';
import { formatFigure } from '../reportFacts';
import { formatCount, formatHours, formatMoney, formatMonthlyMoney, formatPercent, formatRate, formatReportDate, formatShortId } from '../format';

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

// The rule the issue asks for: new figures go through format.js, not toFixed.
// The counts below are the calls that existed when the formatter arrived; a
// file may only go down. Lower a count when you move a call to format.js: the
// test that fails when a call is gone but its count is still listed is meant to
// make you do that in the same change, it is not a bug.
describe('toFixed is not spreading (#2309)', () => {
  const ALLOWED = {
    'components/QueryDecisionTrace.js': 1,
    'pages/AssignmentGate.js': 3,
    'pages/JobMonitoringSummary.js': 2,
    'utils/cacheLayer.js': 4,
    'utils/collectorRowTotals.js': 2,
    'utils/engineChoices.js': 1,
    'utils/tcoAnalysis.js': 2,
  };

  function sources(dir) {
    return fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
      const full = `${dir}/${entry.name}`;
      if (entry.isDirectory()) return entry.name === '__tests__' ? [] : sources(full);
      return entry.name.endsWith('.js') ? [full] : [];
    });
  }

  const root = path.resolve(__dirname, '..', '..');
  const counts = {};
  sources(root).forEach((file) => {
    const n = (fs.readFileSync(file, 'utf8').match(/\.toFixed\(/g) || []).length;
    if (n > 0) counts[file.slice(root.length + 1)] = n;
  });

  it('adds no toFixed call to any file', () => {
    Object.entries(counts).forEach(([file, n]) => {
      expect([file, n <= (ALLOWED[file] || 0)]).toEqual([file, true]);
    });
  });

  it('keeps the allowance honest: it does not list calls that are gone', () => {
    Object.entries(ALLOWED).forEach(([file, n]) => {
      expect([file, counts[file] || 0]).toEqual([file, n]);
    });
  });

  it('format.js is where figures are printed', () => {
    const format = fs.readFileSync(`${root}/utils/format.js`, 'utf8');
    expect(format).toContain('Intl.NumberFormat');
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

  it('prints nothing for a missing or unreadable timestamp, as the cover does', () => {
    expect(formatReportDate(undefined)).toBeNull();
    expect(formatReportDate(null)).toBeNull();
    expect(formatReportDate('')).toBeNull();
    expect(formatReportDate('yesterday')).toBeNull();
    expect(formatReportDate('2026-13-45T00:00:00Z')).toBeNull();
    expect(formatReportDate(1763478647)).toBeNull();
  });
});
