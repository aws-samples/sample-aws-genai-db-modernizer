// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// One place that prints a figure (#2309, #2347). It changes how a number is written,
// never how it is computed: the figures come from the report facts (ADR-032).
// An absent or non-numeric value prints as a dash, never "NaN" or "$NaN".
// No other UI file calls toFixed, toLocaleString or Intl.NumberFormat
// (utils/__tests__/noHandFormatting.test.js).

export const NO_FIGURE = '—';

const MONEY = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' });
const WHOLE_MONEY = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 });
const COUNT = new Intl.NumberFormat('en-US');

function numberOrNull(value) {
  if (value === null || value === undefined || value === '') return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

/**
 * A number as the reports print it (fmt_num in src/shared/figure_format.py): thousands
 * separators, at most `decimals` places with trailing zeros dropped, and a non-zero
 * value too small for them as "<0.01" (">-0.01"), never "0" or "-0". A missing or
 * non-numeric value is NO_FIGURE, never 0.
 */
export function formatFigure(value, decimals = 2) {
  const n = numberOrNull(value);
  if (n === null) return NO_FIGURE;
  const step = 10 ** -decimals;
  if (n !== 0 && Math.abs(n) < step / 2) {
    const smallest = step.toFixed(decimals);
    return n > 0 ? `<${smallest}` : `>-${smallest}`;
  }
  const text = n.toLocaleString('en-US', { minimumFractionDigits: 0, maximumFractionDigits: decimals });
  return text === '-0' ? '0' : text;
}

/** "19.3%", "19%", "<0.1%": fmt_num and a percent sign, as the reports print a share. */
export function formatFigurePercent(value, decimals = 2) {
  const n = numberOrNull(value);
  return n === null ? NO_FIGURE : `${formatFigure(n, decimals)}%`;
}

/** "41.3", "1234.5": a fixed number of places and no separators, Python's ":.Nf". */
export function formatFixed(value, digits) {
  const n = numberOrNull(value);
  return n === null ? NO_FIGURE : n.toFixed(digits);
}

/** "2.0", "1,234.6": a fixed number of places with separators. */
export function formatDecimal(value, digits) {
  const n = numberOrNull(value);
  if (n === null) return NO_FIGURE;
  return new Intl.NumberFormat('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(n);
}

/** "12.3ms": an average latency, one place. */
export function formatMilliseconds(value) {
  const text = formatFixed(value, 1);
  return text === NO_FIGURE ? text : `${text}ms`;
}

/** "185", "0.00422", "29,800": a scale value to `digits` significant digits. */
export function formatSignificant(value, digits = 3) {
  const n = numberOrNull(value);
  return n === null ? NO_FIGURE : n.toLocaleString('en-US', { maximumSignificantDigits: digits });
}

/** A file size: "512 B", "20 KB", "1.2 MB"; '' when there is none. */
export function formatBytes(n) {
  if (typeof n !== 'number' || !Number.isFinite(n)) return '';
  if (n < 1024) return `${n} B`;
  const units = ['KB', 'MB', 'GB'];
  let value = n / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const shown = value >= 10 ? Math.round(value) : Math.round(value * 10) / 10;
  return `${shown} ${units[unit]}`;
}

/** A database size in gigabytes: "12.3 GB" from one gigabyte up, "512 MB" below. */
export function formatDatabaseSize(gb) {
  const n = numberOrNull(gb);
  if (!n) return NO_FIGURE;
  return n >= 1 ? `${n.toFixed(1)} GB` : `${(n * 1024).toFixed(0)} MB`;
}

/** "830", "12.5K", "1.2M": a large count, shortened. */
export function formatCompactCount(value) {
  const n = numberOrNull(value);
  if (n === null) return NO_FIGURE;
  if (n > 1000000) return `${(n / 1000000).toFixed(1)}M`;
  if (n > 1000) return `${(n / 1000).toFixed(1)}K`;
  return COUNT.format(n);
}

/**
 * "$1,178.33", or "$1,178" with `{ whole: true }`. A negative amount puts the sign after
 * the dollar sign, "$-5.00", as the reports' `_fmt_usd` does (src/report/formatting.py).
 */
export function formatMoney(value, { whole = false } = {}) {
  const n = numberOrNull(value);
  if (n === null) return NO_FIGURE;
  const format = whole ? WHOLE_MONEY : MONEY;
  return n < 0 ? `$-${format.format(-n).slice(1)}` : format.format(n);
}

/** "$1,178.33/mo". */
export function formatMonthlyMoney(value, options) {
  const text = formatMoney(value, options);
  return text === NO_FIGURE ? text : `${text}/mo`;
}

/**
 * A rate as the reports and the cost tiles print it (`formatFigure`, the twin of
 * the deliverables' `fmt_num`): "185.07", "29,795.38", "<0.01".
 */
export function formatRate(value) {
  const n = numberOrNull(value);
  return n === null ? NO_FIGURE : formatFigure(n);
}

/** "44.3%" (one decimal by default). */
export function formatPercent(value, digits = 1) {
  const n = numberOrNull(value);
  if (n === null) return NO_FIGURE;
  return `${new Intl.NumberFormat('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(n)}%`;
}

/** "1,234,567". */
export function formatCount(value) {
  const n = numberOrNull(value);
  return n === null ? NO_FIGURE : COUNT.format(n);
}

/** The first eight characters of a job id. */
export function formatShortId(id) {
  return id ? String(id).slice(0, 8) : NO_FIGURE;
}

/** "3.2h": an average duration in hours, one decimal. */
export function formatHours(value) {
  const n = numberOrNull(value);
  return n === null ? NO_FIGURE : `${new Intl.NumberFormat('en-US', { minimumFractionDigits: 1, maximumFractionDigits: 1 }).format(n)}h`;
}

const DATE_TIME = { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit' };

/** "Nov 18, 2026, 10:20:30 AM" in the browser's time zone; null when there is no date. */
export function formatDateTime(value) {
  if (!value) return null;
  return new Date(value).toLocaleString('en-US', DATE_TIME);
}

const REPORT_DATE = new Intl.DateTimeFormat('en-US', { dateStyle: 'medium', timeZone: 'UTC' });

/**
 * "Nov 18, 2026": the day a report was written, from its timestamp's own date part,
 * the day the executive report's cover prints (#2314). Null when there is none.
 */
export function formatReportDate(timestamp) {
  if (typeof timestamp !== 'string') return null;
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(timestamp);
  if (!m) return null;
  const [year, month, day] = [Number(m[1]), Number(m[2]), Number(m[3])];
  const date = new Date(Date.UTC(year, month - 1, day));
  if (date.getUTCFullYear() !== year || date.getUTCMonth() !== month - 1 || date.getUTCDate() !== day) return null;
  return REPORT_DATE.format(date);
}
