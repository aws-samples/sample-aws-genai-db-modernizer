// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2316 (ADR-032): the cost table and its total are facts.engines and
 * facts.engine_cost_total_usd of report.json, the fields the engineering and executive
 * reports print (tests/unit/report/test_cost_table_figures.py checks the engineering
 * report against the same fields of the same file). The page's own TCO figures are not
 * what it prints: when they differ, the facts win.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import CostBreakdown from '../CostBreakdown';
import { formatMoney } from '../../utils/format';

const fs = require('fs');
const path = require('path');
const en = require('../../locales/en.json');

const report = JSON.parse(fs.readFileSync(
  path.join(__dirname, '../../../../../docs/examples/wordpress/sample-run/synthesis/v2/report.json'), 'utf8'));
const t = (key, opts = {}) => {
  const { defaultValue, count, formatted, ...vars } = opts;
  let text = en[key] ?? en[`${key}_${count === 1 ? 'one' : 'other'}`] ?? defaultValue ?? key;
  Object.entries({ count, formatted, ...vars }).forEach(([k, v]) => { text = text.split(`{{${k}}}`).join(v); });
  return text;
};

global.IS_REACT_ACT_ENVIRONMENT = true;
global.ResizeObserver = global.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} };
global.matchMedia = global.matchMedia || (() => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }));

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

test('the rows and the total are the facts the reports print, whatever the TCO says', () => {
  const facts = report.facts;
  // A TCO breakdown that disagrees: only the facts may reach the table.
  const tco = [{ database: 'aurora_mysql', monthly_cost_usd: 1 }];
  act(() => {
    root.render(<CostBreakdown synthesis={report} costBreakdown={tco} projectedCost={2} t={t} />);
  });
  const rows = [...container.querySelectorAll('tbody tr')].map((r) => [...r.querySelectorAll('td')].map((c) => c.textContent));
  expect(rows).toHaveLength(facts.engines.length + 1);
  expect(rows.slice(0, -1).map((r) => r[1])).toEqual(facts.engines.map((e) => e.role));
  expect(rows.slice(0, -1).map((r) => r[5])).toEqual(facts.engines.map((e) => formatMoney(e.monthly_cost_usd)));
  expect(rows[rows.length - 1][0]).toBe('Total');
  expect(rows[rows.length - 1][5]).toBe(formatMoney(facts.engine_cost_total_usd));
  expect(container.querySelector('[data-figure="cost-total-value"]').textContent).toBe(formatMoney(facts.engine_cost_total_usd));
});
