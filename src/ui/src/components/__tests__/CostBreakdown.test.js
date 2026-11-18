// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2316: the cost section on the real Cloudscape components. The total comes first and is
 * the report's own figure; the engines sit in a table with a total row; every note is in one
 * collapsed expandable; the pricing caveat is printed once, with a real external link.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import CostBreakdown from '../CostBreakdown';

const en = require('../../locales/en.json');
const t = (key, opts = {}) => {
  const { defaultValue, count, formatted, ...vars } = opts;
  let text = en[key] ?? en[`${key}_${count === 1 ? 'one' : 'other'}`] ?? defaultValue ?? key;
  Object.entries({ count, formatted, ...vars }).forEach(([k, v]) => { text = text.split(`{{${k}}}`).join(v); });
  return text;
};

global.IS_REACT_ACT_ENVIRONMENT = true;
global.ResizeObserver = global.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} };
global.matchMedia = global.matchMedia || (() => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }));

const CAVEAT = 'Estimates from public on-demand list prices for the stated Region. Check them with your own pricing in the AWS Pricing Calculator (https://calculator.aws/).';

const synthesis = {
  facts: {
    cost_scope_label: 'Full-modernization estimate: all waves done, every kept engine running.',
    cost_caveat: CAVEAT,
    cost_assumptions: [
      'On-demand capacity mode for all target databases',
      'Region: us-east-1',
      'Does not include data transfer, backups, or global tables',
    ],
    cost_basis_notes: ['Reads priced as 95% cache hits'],
    price_list: { source: 'AWS Price List', region: 'us-east-1', pricing_model: 'on-demand', retrieved_on: '2026-10-25' },
    rate_windows: {
      statement: 'execution counts are divided by the server uptime (107 statements), or an assumed 24 hours (0 statements)',
      scenario_label: 'Scenario at the measured rate: the window is under a day',
    },
    growth_scenarios: { lines: ['Cost at 5× growth: $2,000'] },
    engine_cost_total_usd: 507.39,
    engines: [
      { engine: 'aurora_mysql', role: 'Migration target', monthly_cost_usd: 251.24 },
      { engine: 'dynamodb', role: 'Migration target', monthly_cost_usd: 256.15 },
    ],
  },
};
const costBreakdown = [
  { database: 'aurora_mysql', monthly_cost_usd: 251.24, pricing_mode: 'on-demand' },
  { database: 'dynamodb', monthly_cost_usd: 256.15, pricing_mode: 'on-demand' },
];
const withSizing = {
  ...synthesis,
  facts: {
    ...synthesis.facts,
    engines: [
      { ...synthesis.facts.engines[0], cost_sizing: { basis: 'assigned_queries', sized_query_count: 67, calls_per_second: 96.51, unqueried_table_count: 28 } },
      { ...synthesis.facts.engines[1], cost_sizing: { basis: 'assigned_queries', sized_query_count: 40, calls_per_second: 125.53 } },
    ],
  },
};

let container;
let root;
beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});
afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

const render = (props = {}) => act(() => {
  root.render(<CostBreakdown synthesis={withSizing} costBreakdown={costBreakdown} projectedCost={507.39} t={t} {...props} />);
});

test('the total is the first and biggest figure, ahead of the table', () => {
  render();
  const total = container.querySelector('[data-figure="cost-total-value"]');
  expect(total.textContent).toBe('$507.39');
  const table = container.querySelector('table');
  expect(total.compareDocumentPosition(table) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
});

test('the engines are table rows with role, queries, calls per second, sized for and cost, then a total row', () => {
  render();
  const rows = [...container.querySelectorAll('tbody tr')].map((r) => [...r.querySelectorAll('td')].map((c) => c.textContent));
  expect(rows).toHaveLength(3);
  expect(rows[0]).toEqual(['Aurora MySQL', 'Migration target', '67', '96.51', 'Assigned queries, plus 28 tables no query reads', '$251.24']);
  expect(rows[1][0]).toBe('DynamoDB');
  expect(rows[2][0]).toBe('Total');
  expect(rows[2][5]).toBe('$507.39');
  expect(container.querySelector('[data-figure="cost-sizing"][data-engine="dynamodb"]').textContent)
    .toBe('Assigned queries');
  expect(container.textContent).not.toContain('Sized for 40');
});

test('the notes are in one expandable that starts closed, with no run of middle dots', () => {
  render();
  const toggle = container.querySelector('[data-figure="cost-how"] [aria-expanded]') || container.querySelector('[aria-expanded]');
  expect(toggle.getAttribute('aria-expanded')).toBe('false');
  expect(container.textContent).toContain('How these costs are estimated');
  expect(container.textContent).not.toContain(' · ');
  const how = container.querySelector('[data-figure="cost-how"]');
  for (const sel of ['cost-assumptions', 'cost-basis', 'rate-windows', 'rate-window-scenario', 'growth-scenarios']) {
    expect(how.querySelector(`[data-figure="${sel}"]`)).not.toBeNull();
  }
  expect(how.textContent).toContain('on-demand');
  expect(how.textContent).toContain('2026-10-25');
  // Nothing from the notes sits outside it.
  const outside = [...container.querySelectorAll('[data-figure="rate-window-scenario"], [data-figure="cost-assumptions"]')]
    .filter((n) => !how.contains(n));
  expect(outside).toHaveLength(0);
});

test('the pricing caveat is printed once and the calculator is an external link', () => {
  render();
  expect(container.querySelectorAll('[data-figure="cost-caveat"]')).toHaveLength(1);
  const link = container.querySelector('[data-figure="cost-caveat"] a');
  expect(link.getAttribute('href')).toBe('https://calculator.aws/');
  expect(link.getAttribute('target')).toBe('_blank');
  expect(link.textContent).toContain('AWS Pricing Calculator');
  expect(link.querySelector('[role="img"]').getAttribute('aria-label')).toBe('Opens in a new tab');
  expect(container.querySelector('[data-figure="cost-caveat"]').textContent).not.toContain('https://');
  expect(container.textContent.match(/Estimates from public/g)).toHaveLength(1);
});

test('a report with no total still shows the engines, and no notes means no expandable', () => {
  render({ projectedCost: null, synthesis: { facts: { cost_caveat: CAVEAT } } });
  expect(container.querySelector('[data-figure="cost-total"]')).toBeNull();
  expect(container.querySelectorAll('tbody tr')).toHaveLength(2);
  expect(container.querySelector('[aria-expanded]')).toBeNull();
});

test('a report written before the facts falls back to its TCO breakdown and total', () => {
  render({ synthesis: { facts: { cost_caveat: CAVEAT } }, projectedCost: 507.39 });
  expect(container.querySelector('[data-figure="cost-total-value"]').textContent).toBe('$507.39');
  expect([...container.querySelectorAll('tbody tr')].map((r) => r.querySelector('td').textContent)).toEqual(['Aurora MySQL', 'DynamoDB', 'Total']);
});

test('the table has its own name, so a phone-width scroll region is told apart (#2458)', () => {
  render();
  expect(container.querySelector('table').getAttribute('aria-label')).toBe('Cost breakdown');
});
