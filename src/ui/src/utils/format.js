// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// One place that prints a figure (#2309). It changes how a number is written,
// never how it is computed: the figures come from the report facts (ADR-032).
// An absent or non-numeric value prints as a dash, never "NaN" or "$NaN".

import { formatFigure } from './reportFacts';

export const NO_FIGURE = '—';

const MONEY = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' });
const WHOLE_MONEY = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 });
const COUNT = new Intl.NumberFormat('en-US');

function numberOrNull(value) {
  if (value === null || value === undefined || value === '') return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

/** "$1,178.33", or "$1,178" with `{ whole: true }`. */
export function formatMoney(value, { whole = false } = {}) {
  const n = numberOrNull(value);
  if (n === null) return NO_FIGURE;
  return (whole ? WHOLE_MONEY : MONEY).format(n);
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
