// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// One place that prints a figure (#2309). It changes how a number is written,
// never how it is computed: the figures come from the report facts (ADR-032).
// An absent or non-numeric value prints as a dash, never "NaN" or "$NaN".

export const NO_FIGURE = '—';

const MONEY = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' });
const WHOLE_MONEY = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 });
const COUNT = new Intl.NumberFormat('en-US');
const RATE_SMALL = new Intl.NumberFormat('en-US', { maximumSignificantDigits: 3 });
const RATE_LARGE = new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 });

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

/** A rate: whole numbers from 100 up ("29,795"), three significant digits below ("8.28"). */
export function formatRate(value) {
  const n = numberOrNull(value);
  if (n === null) return NO_FIGURE;
  return (Math.abs(n) >= 100 ? RATE_LARGE : RATE_SMALL).format(n);
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
