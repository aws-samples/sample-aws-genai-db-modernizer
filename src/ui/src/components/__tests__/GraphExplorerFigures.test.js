// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2347: the graph explorer prints a query's calls/s and an engine's monthly cost as
 * every other page and the reports print them (formatRate, formatMoney), and an
 * engine's workload share as the reports' fmt_num(x, 1).
 */

import fs from 'fs';
import path from 'path';
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import en from '../../locales/en.json';
import { StepText } from '../GraphExplorerWalkthroughs';
import GraphExplorerQueryPanel from '../GraphExplorerQueryPanel';

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

function t(key, opts = {}) {
  const plural = opts.count === undefined ? null : en[`${key}_${opts.count === 1 ? 'one' : 'other'}`];
  const text = plural || en[key] || key;
  return text.replace(/\{\{(\w+)\}\}/g, (_, name) => String(opts[name] ?? ''));
}

function render(element) {
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  act(() => root.render(element));
  const text = container.textContent;
  act(() => root.unmount());
  container.remove();
  return text;
}

describe('the walkthrough steps print figures as the reports do (#2347)', () => {
  it('prints a query rate with fmt_num', () => {
    const step = { kind: 'query', query: { label: 'Q1', calls_per_second: 0.0042 } };
    expect(render(<StepText t={t} step={step} />)).toContain('Q1: <0.01 calls/s');
  });

  it('prints an engine share with one place and its cost to the cent', () => {
    const step = {
      kind: 'engine', engine_label: 'Amazon DynamoDB', role: 'primary', workload_percent: 44.256,
      monthly_cost_usd: 1178.333, busiest_tables: [{ table: 'wp_posts', query_count: 3, calls_per_second: 185.0712 }],
    };
    const text = render(<StepText t={t} step={step} />);
    expect(text).toContain('Amazon DynamoDB (primary): 44.3% of the workload, $1,178.33/mo');
    expect(text).toContain('wp_posts (3 queries, 185.07 calls/s)');
  });
});

describe('a missing walkthrough share (#2347)', () => {
  it.each([null, undefined])('prints "-" for %p, as the report prints it, never 0%', (share) => {
    const step = { kind: 'engine', engine_label: 'Amazon DynamoDB', role: 'primary', workload_percent: share, monthly_cost_usd: 50, busiest_tables: [] };
    const text = render(<StepText t={t} step={step} />);
    expect(text).toContain('Amazon DynamoDB (primary): - of the workload, $50.00/mo');
    expect(text).not.toContain('0%');
  });

  it('still prints a real zero share as 0%', () => {
    const step = { kind: 'engine', engine_label: 'X', role: 'primary', workload_percent: 0, monthly_cost_usd: 1, busiest_tables: [] };
    expect(render(<StepText t={t} step={step} />)).toContain('X (primary): 0% of the workload');
  });
});

describe('the query panel prints its rate with formatRate (#2347)', () => {
  it('prints 185.07, not 185.071', () => {
    const detail = { query_id: 'q1', operation: 'SELECT', calls_per_second: 185.0712, engine_label: 'Amazon DynamoDB' };
    expect(render(<GraphExplorerQueryPanel t={t} detail={detail} loading={false} error={null} />)).toContain('185.07');
  });
});

describe('the explorer and close-call sources use format.js (#2347)', () => {
  // nosemgrep: javascript.lang.security.audit.path-traversal.path-join-resolve-traversal.path-join-resolve-traversal -- path built from the repo's own tree, not input
  const read = (rel) => fs.readFileSync(path.join(__dirname, '..', '..', rel), 'utf8');

  it('the hover and details cost print through formatMoney', () => {
    const page = read('pages/GraphExplorer.js');
    expect(page).toMatch(/formatMoney\(hovered\.cost\)/);
    expect(page).toMatch(/formatMoney\(selectedNode\.monthly_cost_usd\)/);
    expect(page).toMatch(/formatRate\(selectedBundle\.calls_per_second\)/);
  });

  it('the close-call gap prints as the reports print it, fmt_num(x, 1)', () => {
    expect(read('components/CloseCallCallout.js')).toMatch(/formatFigure\(item\.gap, 1\)/);
  });
});
