// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Each step's why sentence and the order metrics' plain names on the results page
 * (#1699, PR B). The sentence is facts.slice_plan.plan_steps[].why, shown as stored; the
 * metric names are facts.slice_plan.metric_labels, never a copy here: a label the facts
 * do not give is a metric the page leaves out. Both sit in the wave above its step
 * sections (#2286), inside the collapsed wave, so the page opens on the waves alone.
 *
 * #2288: the figures are one Cloudscape table with a row per step, its why sentence in
 * the row; the two 0-to-1 scores read as the facts' band (score_bands, in the words of
 * score_scales) then the stored value, "Low (0.37)", as the engineering report prints them,
 * and every column but the why sentence sorts.
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

const SCALES = {
  // #2288's decision: how tightly tied has a sentence and no band.
  gravity: { high_from: null, band_labels: null, note: 'Gravity note.' },
  hub_score: { high_from: 0.5, band_labels: { low: 'sentinel-low', high: 'sentinel-high' }, note: 'Hub note.' },
};

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
    score_scales: SCALES,
    plan_steps: [
      { step_number: 3, kind: 'slice', engine: 'documentdb', phase_wave: 3, why: WHY_3, metrics: metrics(0.5, 0, 1, 1, 4.1), score_bands: { hub_score: 'low' }, step_stages: [stage('3c', 'with_reverse_replica', ['documentdb:app.profiles'])] },
      { step_number: 4, kind: 'slice', engine: 'dynamodb', phase_wave: 3, why: WHY_4, metrics: metrics(0.62, 0.67, 3, 5, 67.3), score_bands: { hub_score: 'high' }, step_stages: [stage('4a', 'replica_reads', ['dynamodb:app.products'])] },
    ],
    move_units: [
      { key: 'documentdb:app.profiles', query_count: 2, owned_tables: ['app.profiles'], metrics: { gravity: 0.5, hub_score: 0, blast_radius: 2 }, score_bands: { hub_score: 'low' } },
      { key: 'dynamodb:app.products', query_count: 1, owned_tables: [], replicated_in: [{ table: 'app.products', source_of_truth: 'aurora_mysql' }], metrics: { gravity: 1, hub_score: 0.12, blast_radius: 1 }, score_bands: { hub_score: 'low' } },
    ],
    wave_phases: [
      { phase_wave: 3, step_numbers: [3, 4], engines: ['documentdb', 'dynamodb'], stage_labels: ['3c', '4a'], stage_kinds: ['replica_reads', 'with_reverse_replica'], stage_count: 2, query_count: 4 },
    ],
  };
}

function render(p) {
  act(() => { root.render(<WaveStages plan={p} wave={3} path={null} t={t} />); });
}

const all = (name) => [...container.querySelectorAll(`[data-figure="${name}"]`)];

const table = () => all('step-metrics')[0].querySelector('table');
const headerTexts = () => [...table().querySelectorAll('thead th')].map(th => th.textContent.trim());
const bodyRows = () => [...table().querySelectorAll('tbody tr')];
const cellTexts = tr => [...tr.querySelectorAll('th, td')].map(td => td.textContent.trim());

test('the figures are one table, each step a row with its why sentence in it, above the step sections', () => {
  render(plan());
  expect(all('step-metrics')).toHaveLength(1);
  expect(table()).not.toBeNull();
  expect(table().getAttribute('aria-label')).toBe(t('analysis-results.adoption.step-table-label', { wave: 3 }));
  // One row per step, in step order, each holding its own sentence.
  const rows = bodyRows();
  expect(rows).toHaveLength(2);
  expect(rows.map(tr => tr.querySelector('[data-figure="step-why"]').textContent)).toEqual([WHY_3, WHY_4]);
  expect(rows.map(tr => tr.querySelector('[data-figure="step-row"]').textContent)).toEqual(['Step 3: DocumentDB', 'Step 4: DynamoDB']);
  // No separate list of sentences.
  expect(all('step-why-block')[0].querySelector('ul')).toBeNull();
  const why = all('step-why-block')[0];
  const steps = all('wave-step-list')[0];
  expect(why.compareDocumentPosition(steps) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(steps.contains(why)).toBe(false);
});

test('a sentence is shown as stored, as text, never as markup', () => {
  render(plan());
  const [, second] = all('step-why');
  expect(second.textContent).toBe(WHY_4);
  expect(second.querySelector('b')).toBeNull();
});

test('the step figures are headed by the facts metric_labels', () => {
  render(plan());
  // No transactions split and no moved cost: those columns are left out.
  expect(headerTexts()).toEqual([
    t('analysis-results.adoption.step-column'),
    'Sentinel gravity', 'Sentinel hub_score', 'Sentinel replica_table_count',
    'Sentinel write_site_count', 'Sentinel value_call_share_percent',
    t('analysis-results.adoption.why-column'),
  ]);
  // Each metric heading carries its plain name as its tooltip, from the same label.
  expect([...table().querySelectorAll('thead [data-metric]')].map(el => el.title)).toEqual([
    'sentinel gravity', 'sentinel hub_score', 'sentinel replica_table_count',
    'sentinel write_site_count', 'sentinel value_call_share_percent',
  ]);
});

test('a score reads as the facts band, in their words, then its stored value, as plain text', () => {
  render(plan());
  const rows = bodyRows().map(cellTexts);
  expect(rows.map(r => r.slice(0, 6))).toEqual([
    ['Step 3: DocumentDB', '0.5', 'Sentinel-low (0)', '1', '1', '4.1%'],
    ['Step 4: DynamoDB', '0.62', 'Sentinel-high (0.67)', '3', '5', '67.3%'],
  ]);
  const scores = all('step-score');
  expect(scores.map(b => [b.dataset.metric, b.dataset.band, b.dataset.score])).toEqual([
    ['hub_score', 'low', '0'], ['hub_score', 'high', '0.67'],
  ]);
  // How tightly tied has no band: its cells are the plain value.
  expect(all('step-figure').filter(el => el.dataset.metric === 'gravity').map(el => el.textContent)).toEqual(['0.5', '0.62']);
  // Text only, as the engineering report prints it: no badge, no tab stop, no tooltip.
  scores.forEach(el => {
    expect(el.querySelector('[tabindex]')).toBeNull();
    expect(el.hasAttribute('tabindex')).toBe(false);
  });
  expect(container.querySelector('[class*="badge"]')).toBeNull();
  // The band is the facts' score_bands, never a comparison here: a stored band that
  // disagrees with the value is shown as stored.
  const p = plan();
  p.plan_steps[0].score_bands.hub_score = 'high';
  render(p);
  expect(all('step-score')[0].dataset.band).toBe('high');
});

test('each row is named by its step for a screen reader', () => {
  render(plan());
  const headers = bodyRows().map(tr => tr.querySelector('th[scope="row"]'));
  expect(headers.map(th => th && th.textContent.trim())).toEqual(['Step 3: DocumentDB', 'Step 4: DynamoDB']);
});

test('a plan stored with no band shows the raw values', () => {
  const p = plan();
  delete p.score_scales;
  render(p);
  expect(all('step-score')).toHaveLength(0);
  expect(cellTexts(bodyRows()[0]).slice(1, 3)).toEqual(['0.5', '0']);
});

test('every figure column sorts by its stored value', () => {
  render(plan());
  const header = name => [...table().querySelectorAll('thead th')].find(th => th.textContent.trim() === name);
  const steps = () => bodyRows().map(tr => tr.querySelector('[data-figure="step-row"]').dataset.step);
  expect(steps()).toEqual(['3', '4']);
  // Shared by many parts, ascending then descending.
  const hub = () => header('Sentinel hub_score').querySelector('[role="button"]');
  act(() => { hub().click(); });
  expect(steps()).toEqual(['3', '4']);
  act(() => { hub().click(); });
  expect(steps()).toEqual(['4', '3']);
  // The step column sorts too; the why column does not.
  const step = () => header(t('analysis-results.adoption.step-column')).querySelector('[role="button"]');
  act(() => { step().click(); });
  expect(steps()).toEqual(['3', '4']);
  expect(header(t('analysis-results.adoption.why-column')).querySelector('[role="button"]')).toBeNull();
});

test('no default wording names a metric: every name is the facts own', () => {
  render(plan());
  const headings = table().querySelector('thead').textContent;
  ['how tightly tied', 'shared by many parts', 'tables copied across', 'what the move touches']
    .forEach(words => expect(headings.toLowerCase()).not.toContain(words));
});

test('a unit line states its metrics under the facts labels, a score as its band and value', () => {
  render(plan());
  const line = all('stage-unit')[0].textContent;
  expect(line).toContain('sentinel gravity: 0.5;');
  expect(line).toContain('sentinel hub_score: sentinel-low (0)');
  expect(line).toContain('sentinel blast_radius: 2');
});

test('a plan with no labels shows the sentences and no metric it cannot name', () => {
  render(plan({}));
  expect(all('step-why')).toHaveLength(2);
  expect(headerTexts()).toEqual([
    t('analysis-results.adoption.step-column'), t('analysis-results.adoption.why-column'),
  ]);
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
  p.plan_steps[1].score_bands.hub_score = 'low';
  render(p);
  expect(cellTexts(bodyRows()[1]).slice(0, 6)).toEqual(['Step 4: DynamoDB', '0.62', 'Sentinel-low (<0.01)', '3', '1,234', '67.3%']);
});
