// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Each step's why sentence and the order metrics' plain names on the results page
 * (#1699, PR B). The sentence is facts.slice_plan.plan_steps[].why, shown as stored; the
 * metric names are facts.slice_plan.metric_labels, never a copy here: a label the facts
 * do not give is a metric the page leaves out. Both sit inside the wave's collapsed
 * stage list, so the page opens on the same view as before (#1699's decisions).
 */

import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { createInstance } from 'i18next';
import en from '../../locales/en.json';
import WaveStages from '../WaveStages';
import { formatFigure } from '../../utils/reportFacts';
import { metricValueText } from '../../utils/adoptionPaths';

jest.mock('@cloudscape-design/components/box', () => ({
  __esModule: true,
  default: ({ children }) => <div>{children}</div>,
}));

global.IS_REACT_ACT_ENVIRONMENT = true;

let container;
let root;
let t;

beforeAll(async () => {
  const i18n = createInstance();
  await i18n.init({
    resources: { en: { translation: en } },
    lng: 'en',
    fallbackLng: 'en',
    keySeparator: false,
    interpolation: { escapeValue: false },
  });
  t = i18n.t.bind(i18n);
});

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

const WHY_3 = 'Step 3 moves 2 queries and 1 table to DocumentDB, 4.1% of calls, in wave 3 of the roadmap.';
const WHY_4 = 'Step 4 moves 8 queries <b>and</b> 4 tables to DynamoDB, 67.3% of calls.';

function labels(prefix) {
  return Object.fromEntries([
    'gravity', 'hub_score', 'blast_radius', 'cut_cost', 'replica_table_count', 'write_site_count',
    'transactions_split', 'value_call_share_percent', 'value_monthly_cost_usd',
  ].map(k => [k, `${prefix} ${k}`]));
}

function plan(metricLabels = labels('sentinel')) {
  const stage = (label, kind, keys) => ({
    label, kind, unit_keys: keys, unit_count: keys.length, query_count: 2, gate: 'Gate.',
  });
  const metrics = (gravity, hub, copies, writes, share) => ({
    gravity,
    hub_score: hub,
    cut_cost: { replica_table_count: copies, write_site_count: writes, transactions_split: null },
    value_call_share_percent: share,
    value_monthly_cost_usd: null,
  });
  return {
    metric_labels: metricLabels,
    plan_steps: [
      { step_number: 3, kind: 'slice', engine: 'documentdb', phase_wave: 3, why: WHY_3, metrics: metrics(0.5, 0, 1, 1, 4.1), step_stages: [stage('3c', 'with_reverse_replica', ['documentdb:app.profiles'])] },
      { step_number: 4, kind: 'slice', engine: 'dynamodb', phase_wave: 3, why: WHY_4, metrics: metrics(0.62, 0.67, 3, 5, 67.3), step_stages: [stage('4a', 'replica_reads', ['dynamodb:app.products'])] },
    ],
    move_units: [
      { key: 'documentdb:app.profiles', query_count: 2, owned_tables: ['app.profiles'], metrics: { gravity: 0.5, hub_score: 0, blast_radius: 2 } },
      { key: 'dynamodb:app.products', query_count: 1, owned_tables: [], replicated_in: [{ table: 'app.products', source_of_truth: 'aurora_mysql' }], metrics: { gravity: 1, hub_score: 0.12, blast_radius: 1 } },
    ],
    wave_phases: [
      { phase_wave: 3, step_numbers: [3, 4], engines: ['documentdb', 'dynamodb'], stage_labels: ['3c', '4a'], stage_kinds: ['replica_reads', 'with_reverse_replica'], stage_count: 2, query_count: 4 },
    ],
  };
}

function render(p) {
  act(() => { root.render(<WaveStages plan={p} wave={3} path={null} t={t} />); });
}

function open() {
  const button = container.querySelector('[data-disclosure="wave-stage-list"]');
  act(() => { button.dispatchEvent(new MouseEvent('click', { bubbles: true })); });
}

const all = (name) => [...container.querySelectorAll(`[data-figure="${name}"]`)];

test('the sentences sit inside the collapsed stage list, so the default view is unchanged', () => {
  render(plan());
  expect(all('step-why')).toHaveLength(0);
  expect(container.textContent).not.toContain(WHY_3);
  open();
  expect(all('step-why').map(el => el.textContent)).toEqual([WHY_3, WHY_4]);
});

test('a sentence is shown as stored, as text, never as markup', () => {
  render(plan());
  open();
  const [, second] = all('step-why');
  expect(second.textContent).toBe(WHY_4);
  expect(second.querySelector('b')).toBeNull();
});

test('the step figures are headed by the facts metric_labels', () => {
  render(plan());
  open();
  const table = all('step-metrics')[0];
  const headers = [...table.querySelectorAll('th')].map(th => th.textContent);
  // No transactions split and no moved cost: those columns are left out.
  expect(headers).toEqual([
    t('analysis-results.adoption.step-column'),
    'Sentinel gravity', 'Sentinel hub_score', 'Sentinel replica_table_count',
    'Sentinel write_site_count', 'Sentinel value_call_share_percent',
  ]);
  const rows = [...table.querySelectorAll('tbody tr')].map(tr => [...tr.children].map(td => td.textContent));
  expect(rows).toEqual([
    ['3', '0.5', '0', '1', '1', '4.1%'],
    ['4', '0.62', '0.67', '3', '5', '67.3%'],
  ]);
  // Each heading carries its plain name as its tooltip, from the same label.
  expect([...table.querySelectorAll('th[title]')].map(th => th.title)).toEqual(headers.slice(1).map(h => h.toLowerCase()));
});

test('no default wording names a metric: every name is the facts own', () => {
  render(plan());
  open();
  expect(all('step-metrics')).toHaveLength(1);
  const headings = all('step-metrics').map(el => el.querySelector('thead').textContent).join(' ');
  ['how tightly tied', 'shared by many parts', 'tables copied across', 'what the move touches']
    .forEach(words => expect(headings.toLowerCase()).not.toContain(words));
});

test('a unit line states its metrics under the facts labels', () => {
  render(plan());
  open();
  const units = container.querySelector('[data-disclosure="stage-units"]');
  act(() => { units.dispatchEvent(new MouseEvent('click', { bubbles: true })); });
  const line = all('stage-unit')[0].textContent;
  expect(line).toContain('sentinel gravity: 0.5');
  expect(line).toContain('sentinel hub_score: 0');
  expect(line).toContain('sentinel blast_radius: 2');
});

test('a plan with no labels shows the sentences and no metric it cannot name', () => {
  render(plan({}));
  open();
  expect(all('step-why')).toHaveLength(2);
  expect(all('step-metrics')).toHaveLength(0);
});

test('a metric reads as the reports print it (fmt_num)', () => {
  // fmt_num: thousands separators, at most two places, a tiny non-zero value as "<0.01".
  expect(formatFigure(1234)).toBe('1,234');
  expect(formatFigure(0.004)).toBe('<0.01');
  expect(formatFigure(-0.004)).toBe('>-0.01');
  expect(formatFigure(0)).toBe('0');
  expect(formatFigure(0.625)).toBe('0.63');
  expect(metricValueText('write_site_count', 1234)).toBe('1,234');
  expect(metricValueText('gravity', 0.004)).toBe('<0.01');
  expect(metricValueText('value_call_share_percent', 0.004)).toBe('<0.01%');
  expect(metricValueText('value_call_share_percent', 1234.5)).toBe('1,234.5%');
});

test('the figures table prints large and tiny values as the reports do', () => {
  const p = plan();
  p.plan_steps[1].metrics.cut_cost.write_site_count = 1234;
  p.plan_steps[1].metrics.hub_score = 0.004;
  render(p);
  open();
  const row = [...all('step-metrics')[0].querySelectorAll('tbody tr')][1];
  expect([...row.children].map(td => td.textContent)).toEqual(['4', '0.62', '<0.01', '3', '1,234', '67.3%']);
});
