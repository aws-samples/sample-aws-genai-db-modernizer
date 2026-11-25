// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2318: the Plan tab's roadmap, on the real Cloudscape components and the real English
 * strings. Close calls are one table (a row per query, a filter, a pager), a wave's
 * readiness is a status with the facts' own lists under it, the path choice is a
 * segmented control, the routines and the automatic engine choices sit in wave 1, and the
 * cutover sentence is said once, in the roadmap's header.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import i18n from '../../i18n';
import CloseCallCallout from '../CloseCallCallout';
import MigrationWavePlan from '../MigrationWavePlan';
import routines from './fixtures/routineFacts.json';
import { closeCallRows } from '../../utils/closeCalls';
import { filterQueryRows } from '../../utils/queryRouting';

global.IS_REACT_ACT_ENVIRONMENT = true;
global.ResizeObserver = global.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} };
global.matchMedia = global.matchMedia || (() => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }));

const t = i18n.t.bind(i18n);
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
const wrapper = () => createWrapper(container);

// ---------------------------------------------------------------------------
// Close calls
// ---------------------------------------------------------------------------

const ID_A = '-3536340aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa';
const ID_B = '00112233bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb';
const ID_C = '99887766cccccccccccccccccccccccccccccccccccccccccccccccccccccccc';

function closeCallSynthesis(rows = null) {
  const listed = rows || [
    { query_id: ID_B, engine: 'aurora_mysql', alternative: 'dynamodb', gap: 3.5, basis: 'query', settled_tie: false },
    { query_id: ID_A, engine: 'dynamodb', alternative: 'aurora_mysql', gap: 0.5, basis: 'query', settled_tie: false },
    { query_id: ID_C, engine: 'dynamodb', alternative: 'aurora_mysql', gap: 0, basis: 'query', settled_tie: true },
  ];
  return {
    facts: {
      close_calls: {
        margin_points: 5,
        close_call_query_count: 2,
        settled_tie_count: 1,
        engine_pairs: [
          { engine: 'dynamodb', alternative: 'aurora_mysql', pair_query_count: 1, exact_tie_count: 1, group_mean_count: 0, min_gap: 0.5, max_gap: 0.5, query_ids: [ID_A, ID_C], more_query_count: 0 },
          { engine: 'aurora_mysql', alternative: 'dynamodb', pair_query_count: 1, exact_tie_count: 0, group_mean_count: 0, min_gap: 3.5, max_gap: 3.5, query_ids: [ID_B], more_query_count: 0 },
        ],
      },
    },
    query_traces: { close_calls: listed },
  };
}
const queries = [
  { queryId: ID_A, statement: 'SELECT * FROM wp_options WHERE option_name = ?' },
  { queryId: ID_B, statement: 'UPDATE wp_posts SET post_title = ?' },
];
const renderCloseCalls = (props = {}) => act(() => {
  root.render(
    <CloseCallCallout
      synthesis={closeCallSynthesis()}
      jobId="job-1"
      onOpen={() => {}}
      queries={queries}
      t={t}
      {...props}
    />,
  );
});
const bodyRows = () => wrapper().findTable().findRows().map(r => [...r.getElement().querySelectorAll('td')].map(c => c.textContent));

test('close calls are a table with a row per query, closest first and the settled ties last', () => {
  renderCloseCalls();
  expect(container.querySelector('#close-calls-section')).not.toBeNull();
  expect(wrapper().findTable()).not.toBeNull();
  const rows = bodyRows();
  expect(rows).toHaveLength(3);
  // query, statement, assigned engine, runner-up, gap, settled by
  expect(rows[0]).toEqual(['-3536340', 'SELECT * FROM wp_options WHERE option_name = ?', 'DynamoDB', 'Aurora MySQL', '0.5', 'Score']);
  expect(rows[1][0]).toBe('00112233');
  expect(rows[1][4]).toBe('3.5');
  expect(rows[2].slice(4)).toEqual(['0', 'Tie rule']);
  // A query the page has not loaded shows a dash for its statement.
  expect(rows[2][1]).toBe('—');
});

test('an id reads as in the routing table and the reports: its first eight characters, the whole id in its tooltip and label', () => {
  renderCloseCalls();
  const link = container.querySelector(`[title="${ID_A}"]`);
  expect(link.textContent).toBe('-3536340');
  const button = container.querySelector(`a[aria-label="Open the decision trace for query ${ID_A}"]`);
  expect(button).not.toBeNull();
});

test('an id opens that query\'s decision trace', () => {
  const opened = [];
  renderCloseCalls({ onOpen: id => opened.push(id) });
  act(() => {
    container.querySelector(`a[aria-label="Open the decision trace for query ${ID_B}"]`).click();
  });
  expect(opened).toEqual([ID_B]);
});

test('the header counts the close calls, says the margin once and links to the assignment review', () => {
  renderCloseCalls();
  const header = container.querySelector('h2');
  expect(header.textContent).toContain('Close calls');
  expect(header.textContent).toContain('(3)');
  const description = container.textContent;
  expect(description).toContain('An engine won each of these queries by less than 5 points over a runner-up the plan keeps.');
  expect(description).toContain('1 of them is an exact tie that a routing rule settled.');
  const link = [...container.querySelectorAll('a')].find(a => a.textContent === 'Open the assignment review');
  expect(link.getAttribute('href')).toBe('/analysis/assignments/job-1');
  // The old callout's paragraphs, "Closest:" lists and "+N more" toggles are gone.
  expect(container.textContent).not.toContain('Closest:');
  expect(container.textContent).not.toMatch(/\+\d+ more/);
  expect(container.querySelector('[role="alert"]')).toBeNull();
});

test('without ties the header says nothing about them', () => {
  const synthesis = closeCallSynthesis();
  synthesis.facts.close_calls.settled_tie_count = 0;
  renderCloseCalls({ synthesis });
  expect(container.textContent).not.toContain('exact tie');
});

test('the filter narrows the table by engine, runner-up and what settled it', () => {
  renderCloseCalls();
  const filter = wrapper().findPropertyFilter();
  act(() => filter.setInputValue('tie rule'));
  expect(filter.findNativeInput().getElement()).toBeTruthy();
  // A free-text token searches the id and the statement.
  act(() => filter.setInputValue('wp_posts'));
  act(() => { filter.findNativeInput().keydown({ keyCode: 13, key: 'Enter' }); });
  expect(bodyRows()).toHaveLength(1);
  expect(bodyRows()[0][0]).toBe('00112233');
  expect(container.querySelector('h2').textContent).toContain('(1 of 3)');
});

test('the filter properties match the assigned engine, the runner-up and what settled the query', () => {
  const rows = closeCallRows(closeCallSynthesis(), queries);
  const by = (propertyKey, value, operator = '=') => filterQueryRows(rows, {
    operation: 'and', tokens: [{ propertyKey, operator, value }],
  }).map(r => r.queryId);
  expect(by('settledBy', 'tie-rule')).toEqual([ID_C]);
  expect(by('settledBy', 'score')).toEqual([ID_A, ID_B]);
  expect(by('engine', 'aurora_mysql')).toEqual([ID_B]);
  expect(by('runnerUp', 'aurora_mysql')).toEqual([ID_A, ID_C]);
  expect(by('runnerUp', 'aurora_mysql', '!=')).toEqual([ID_B]);
});

test('the table pages 25 rows at a time', () => {
  const many = Array.from({ length: 60 }, (_, i) => ({
    query_id: `${String(1000 + i)}${'d'.repeat(60)}`,
    engine: 'dynamodb', alternative: 'aurora_mysql', gap: 1 + (i / 100), basis: 'query', settled_tie: false,
  }));
  renderCloseCalls({ synthesis: closeCallSynthesis(many) });
  expect(bodyRows()).toHaveLength(25);
  expect(wrapper().findPagination().findPageNumbers()).toHaveLength(3);
});

test('a group-mean close call is settled by its group score', () => {
  const rows = closeCallRows(closeCallSynthesis([
    { query_id: ID_A, engine: 'dynamodb', alternative: 'aurora_mysql', gap: 1, basis: 'group_mean', settled_tie: false },
  ]), []);
  expect(rows.map(r => r.settledBy)).toEqual(['group-score']);
});

test('a report with no per-query rows lists the ids its facts name, with no gap', () => {
  const synthesis = closeCallSynthesis();
  delete synthesis.query_traces;
  renderCloseCalls({ synthesis });
  const rows = bodyRows();
  expect(rows).toHaveLength(3);
  expect(rows.map(r => r[4])).toEqual(['—', '—', '—']);
  // No total for rows the report does not list; each pair says how many more it has.
  expect(container.querySelector('h2').textContent).not.toMatch(/\(\d+\)/);
  const more = synthesis.facts.close_calls.engine_pairs[0];
  more.more_query_count = 10;
  renderCloseCalls({ synthesis });
  expect(container.textContent).toContain('10 more close calls to DynamoDB over Aurora MySQL are not listed in this report.');
  more.more_query_count = 1;
  renderCloseCalls({ synthesis });
  expect(container.textContent).toContain('1 more close call to DynamoDB over Aurora MySQL is not listed in this report.');
});

test('a report that lists every close call says nothing about unlisted ones', () => {
  const synthesis = closeCallSynthesis();
  synthesis.facts.close_calls.engine_pairs[0].more_query_count = 10;
  renderCloseCalls({ synthesis });
  expect(container.textContent).not.toContain('not listed in this report');
});

test('a hostile query id is shown as text, never as markup', () => {
  const hostile = 'q-<b>x</b>-----------------------------------';
  renderCloseCalls({ synthesis: closeCallSynthesis([
    { query_id: hostile, engine: 'dynamodb', alternative: 'aurora_mysql', gap: 1, basis: 'query', settled_tie: false },
  ]) });
  expect(container.querySelector('b')).toBeNull();
  expect(container.textContent).toContain('q-<b>x</');
});

test('no section with no synthesis, or a facts block that counts and lists nothing', () => {
  renderCloseCalls({ synthesis: null });
  expect(container.innerHTML).toBe('');
  renderCloseCalls({ synthesis: { facts: { close_calls: { margin_points: 5, close_call_query_count: 0, settled_tie_count: 0, engine_pairs: [] } } } });
  expect(container.innerHTML).toBe('');
});

test('a statement is shown as text, never as markup', () => {
  const hostile = '<img src=x onerror=alert(1)>';
  renderCloseCalls({ queries: [{ queryId: ID_A, statement: hostile }] });
  expect(container.querySelector('img')).toBeNull();
  expect(container.textContent).toContain(hostile);
});

test('no section when the report has no close calls', () => {
  renderCloseCalls({ synthesis: { facts: {} } });
  expect(container.innerHTML).toBe('');
});

// ---------------------------------------------------------------------------
// The roadmap
// ---------------------------------------------------------------------------

function stage(label) {
  return {
    label, kind: 'whole_database', unit_keys: ['aurora_mysql:whole'], unit_count: 1, query_count: 3,
    owned_table_count: 1, call_share_percent: 1, holding_risk_ids: [], gate: `Gate ${label}.`,
  };
}
function adoptionPath(id, stop, extra = {}) {
  return {
    path_id: id, engines: ['aurora_mysql'], stops_after_step: stop, step_count: stop, stage_count: stop,
    gate_count: stop, queries_moved: 10 * stop, queries_by_engine: {}, monthly_cost_usd: 100 + stop,
    engine_costs: [], cost_delta_vs_relational_usd: 0, cost_delta_vs_current_usd: null,
    cost_is_upper_bound: false, critical_high_risks: 1, replicated_tables: [],
    replicated_table_count: 0, temporary_stream_count: 0, first_gate: 'Full load complete.', ...extra,
  };
}
const wave = (n, engines = ['aurora_mysql']) => ({
  wave: n, title: `Move ${n}`, engines, query_count: 3, workload_share_percent: 10, table_count: 1,
});

function roadmap({ waveFacts = [], withAdoption = true, extraFacts = {} } = {}) {
  return {
    migration_waves: [wave(1), wave(2, ['dynamodb'])],
    facts: {
      migration_wave_count: 2,
      waves: waveFacts,
      cutover_responsibility: 'The cutover is your responsibility.',
      ...(withAdoption ? {
        adoption: {
          default_path_id: 'full_target',
          path_count: 3,
          summary_path_ids: ['relational_only', 'through_step_2', 'full_target'],
          cost_basis: 'per_path_sizing',
          paths: [adoptionPath('relational_only', 1), adoptionPath('through_step_2', 2), adoptionPath('full_target', 3)],
        },
        slice_plan: { plan_steps: [
          { step_number: 1, kind: 'relational_step', engine: 'aurora_mysql', phase_wave: 1, step_stages: [stage('1')] },
          { step_number: 2, kind: 'slice', engine: 'dynamodb', phase_wave: 2, step_stages: [stage('2a')] },
          { step_number: 3, kind: 'slice', engine: 'dynamodb', phase_wave: 2, step_stages: [stage('3a')] },
        ], wave_phases: [] },
      } : {}),
      ...extraFacts,
    },
  };
}
const renderRoadmap = (synthesis) => act(() => {
  root.render(<MigrationWavePlan synthesis={synthesis} t={t} />);
});
const waveCard = n => container.querySelector(`[data-wave="${n}"]`);

test('the cutover sentence is said once, in the roadmap header', () => {
  renderRoadmap(roadmap({ extraFacts: { routines } }));
  const sentences = container.textContent.split('The cutover is your responsibility.').length - 1;
  expect(sentences).toBe(1);
  const header = container.querySelector('h2').closest('div[class*="header"]');
  expect(header.querySelector('[data-figure="cutover-responsibility"]').textContent)
    .toBe('The cutover is your responsibility.');
});

const SUGGESTED = {
  wave: 1, readiness: 'suggested', suggested: true, holds: [], next_steps: [], to_dos: [],
  readiness_line: 'Suggested, with 2 design review warnings to accept: DynamoDB DDB-AP-56: add an index; DynamoDB DDB-AP-57: widen a key. If you do not accept them: revise the DynamoDB schema design in your modernization session and review it again.',
  review_warnings: [
    { engine: 'dynamodb', target: 'DDB-AP-56', change: 'add an index.' },
    { engine: 'dynamodb', target: 'DDB-AP-57', change: 'widen a key' },
  ],
  review_warning_count: 2, open_warning_count: 2, accepted_warning_count: 0, rejected_warning_count: 0,
};
const readinessOf = (n = 1) => waveCard(n).querySelector('[data-figure="wave-readiness"]');
const lineOf = (n = 1) => readinessOf(n).querySelector('[data-figure="wave-readiness-line"]').textContent;
// #2287: the readiness status is stated once, in the wave's header; the body keeps the line.
const statusOf = (n = 1) => {
  expect(createWrapper(readinessOf(n)).findStatusIndicator()).toBeNull();
  return createWrapper(waveCard(n).querySelector('[data-figure="wave-header-readiness"]')).findStatusIndicator();
};

test('a suggested wave with warnings is a status beside the facts\' line, shown whole', () => {
  renderRoadmap(roadmap({ waveFacts: [SUGGESTED] }));
  expect(readinessOf().getAttribute('data-readiness')).toBe('suggested');
  const status = statusOf();
  expect(status.getElement().textContent).toBe('Suggested, 2 warnings to accept');
  expect(status.getElement().className).toMatch(/warning/);
  expect(lineOf()).toBe(SUGGESTED.readiness_line);
});

test('a suggested wave with nothing open is a success status, with its line whole', () => {
  const line = 'Suggested: no check holds it after it was redesigned twice.';
  renderRoadmap(roadmap({ waveFacts: [{ ...SUGGESTED, review_warnings: [], review_warning_count: 0, open_warning_count: 0, readiness_line: line }] }));
  const status = statusOf();
  expect(status.getElement().textContent).toBe('Suggested');
  expect(status.getElement().className).toMatch(/success/);
  expect(lineOf()).toBe(line);
});

test.each([
  ['held', 'Not suggested yet', 'Not suggested yet (redesigned twice, still held): an open high risk on DynamoDB (RISK-001). Next step: run the DynamoDB schema design. To do: migrate 5 routines yourself.'],
  ['not_designed', 'Not designed', 'Not designed: the DynamoDB design was skipped (no model). Run the DynamoDB schema design, then check the wave again.'],
  ['not_reviewed', 'Not reviewed', 'Not reviewed: the DynamoDB design was not reviewed (the review failed). Next step: review it again, then check the wave again.'],
])('a %s wave shows its status and every part of the facts\' line', (readiness, status, line) => {
  renderRoadmap(roadmap({ waveFacts: [{ wave: 1, readiness, open_warning_count: 0, readiness_line: line }] }));
  expect(statusOf().getElement().textContent).toBe(status);
  expect(lineOf()).toBe(line);
});

test('a decision on the warnings keeps the facts\' line as it is', () => {
  const line = 'Suggested: you accepted 2 design review warnings with a wave edit.';
  renderRoadmap(roadmap({ waveFacts: [{ ...SUGGESTED, open_warning_count: 0, accepted_warning_count: 2, readiness_line: line }] }));
  expect(lineOf()).toBe(line);
});

test('the path choice is a segmented control, and choosing a path changes only the page', () => {
  renderRoadmap(roadmap());
  const control = wrapper().findSegmentedControl();
  expect(control).not.toBeNull();
  expect(control.findSelectedSegment().getElement().textContent).toBe('Full target, the default');
  expect(container.querySelector('[data-figure="adoption-paths"] [role="group"]')).toBeNull();
  const labels = control.findSegments().map(s => s.getElement().textContent);
  expect(labels).toEqual(['Relational move only', 'Through step 2', 'Full target, the default']);
  act(() => control.findSegments()[0].click());
  expect(wrapper().findSegmentedControl().findSelectedSegment().getElement().textContent).toBe('Relational move only');
  expect(container.querySelector('[data-figure="selected-path"]').getAttribute('data-path')).toBe('relational_only');
});

test('the one-step option\'s header sits under the adoption paths heading, not beside the wave titles', () => {
  const synthesis = roadmap();
  synthesis.facts.adoption.one_step = {
    staged_step_count: 3, staged_stage_count: 3, gate_count: 3, suits_one_step: true, one_step_checks: [],
  };
  renderRoadmap(synthesis);
  const option = container.querySelector('[data-figure="one-step-option"]');
  expect(option.querySelector('h4')).not.toBeNull();
  expect(option.querySelector('h2')).toBeNull();
});

const WORDS = routines;
test('the routines sit in wave 1 only, as the facts state them', () => {
  renderRoadmap(roadmap({ extraFacts: { routines: WORDS } }));
  expect(container.querySelectorAll('#routines-section')).toHaveLength(1);
  expect(waveCard(1).querySelector('#routines-section')).not.toBeNull();
  expect(waveCard(2).querySelector('#routines-section')).toBeNull();
  expect(waveCard(1).querySelector('[data-figure="routines-line"]').textContent).toBe(WORDS.line);
  // The routine detail is a collapsed footer, not a container of its own.
  expect(container.querySelector('#routines-section [class*="variant-container"]')).toBeNull();
});

test('the automatic engine choices sit in wave 1 as a status line, not an alert', () => {
  renderRoadmap(roadmap({ extraFacts: {
    auto_decided_choice_count: 2, auto_decided_table_count: 2, auto_decided_no_table_count: 0, auto_decided_query_count: 5,
  } }));
  const note = waveCard(1).querySelector('#engine-choices-note');
  expect(note).not.toBeNull();
  expect(note.textContent).toContain('decided automatically');
  expect(waveCard(2).querySelector('#engine-choices-note')).toBeNull();
  expect(wrapper().findAlert()).toBeNull();
});

test('no routines and no choices leave wave 1 without either', () => {
  renderRoadmap(roadmap());
  expect(container.querySelector('#routines-section')).toBeNull();
  expect(container.querySelector('#engine-choices-note')).toBeNull();
});

test('with no waves, the routines and the pending engine choices still show, on their own', () => {
  renderRoadmap({
    migration_waves: [],
    facts: {
      routines,
      pending_choice_count: 2, pending_choice_table_count: 2, pending_choice_no_table_count: 0, pending_choice_query_count: 5,
    },
  });
  expect(container.querySelector('#routines-section')).not.toBeNull();
  expect(container.querySelector('#engine-choices-note').textContent).toContain('still tie between engines');
  expect(container.querySelector('#migration-roadmap-section')).toBeNull();
  renderRoadmap({ migration_waves: [], facts: {} });
  expect(container.innerHTML).toBe('');
});

// ---------------------------------------------------------------------------
// #2286: waves, steps and stages as nested sections
// ---------------------------------------------------------------------------

function nestedRoadmap() {
  const synthesis = roadmap({ waveFacts: [{ ...SUGGESTED, wave: 2, served_tables: 224 }] });
  synthesis.migration_waves[1] = { ...wave(2, ['dynamodb', 'documentdb']), query_count: 857, table_count: 224, workload_share_percent: 40 };
  const plan = synthesis.facts.slice_plan;
  plan.plan_steps[1] = { ...plan.plan_steps[1], stage_count: 1, query_count: 3 };
  plan.plan_steps[2] = { ...plan.plan_steps[2], engine: 'documentdb', stage_count: 1, query_count: 3 };
  plan.wave_phases = [
    { phase_wave: 1, step_numbers: [1], engines: ['aurora_mysql'], stage_labels: ['1'], stage_kinds: ['whole_database'], stage_count: 1, query_count: 3 },
    { phase_wave: 2, step_numbers: [2, 3], engines: ['dynamodb', 'documentdb'], stage_labels: ['2a', '3a'], stage_kinds: ['whole_database'], stage_count: 2, query_count: 6 },
  ];
  return synthesis;
}
const sectionOf = el => createWrapper(el).findExpandableSection();
const isOpen = section => section.findExpandButton().getElement().getAttribute('aria-expanded') === 'true';

test('each wave is a container section headed "Wave N: title", one level under the roadmap, collapsed', () => {
  renderRoadmap(nestedRoadmap());
  [1, 2].forEach((n) => {
    const section = sectionOf(waveCard(n));
    expect(section).not.toBeNull();
    expect(section.findHeaderText().getElement().textContent).toBe(`Wave ${n}: Move ${n}`);
    expect(section.findHeaderText().getElement().closest('h3')).not.toBeNull();
    expect(isOpen(section)).toBe(false);
  });
  expect(container.querySelector('h2').textContent).toContain('Migration roadmap');
});

test('a collapsed wave states its engines, query and table counts and readiness in its header', () => {
  renderRoadmap(nestedRoadmap());
  const header = sectionOf(waveCard(2)).findHeader().getElement();
  expect([...header.querySelectorAll('[data-engine]')].map(e => e.getAttribute('data-engine'))).toEqual(['dynamodb', 'documentdb']);
  // The reports' headline, as the body printed it before (#957).
  expect(header.querySelector('[data-figure="wave-stats"]').textContent).toBe('857 queries (40% of queries), 224 source tables');
  const readiness = header.querySelector('[data-figure="wave-header-readiness"]');
  expect(readiness.getAttribute('data-readiness')).toBe('suggested');
  expect(createWrapper(readiness).findStatusIndicator().getElement().textContent).toBe('Suggested, 2 warnings to accept');
  // Wave 1 has no readiness in its facts: its header states none.
  expect(sectionOf(waveCard(1)).findHeader().getElement().querySelector('[data-figure="wave-header-readiness"]')).toBeNull();
  // The gate and the readiness line stay in the body.
  expect(header.querySelector('[data-figure="wave-gate"], [data-figure="wave-readiness-line"]')).toBeNull();
  expect(sectionOf(waveCard(2)).findContent().getElement().querySelector('[data-figure="wave-readiness-line"]')).not.toBeNull();
});

test('a wave, its steps and its stages open and close with the keyboard, one level at a time', () => {
  renderRoadmap(nestedRoadmap());
  const wave2 = sectionOf(waveCard(2));
  const step = sectionOf(waveCard(2).querySelector('[data-plan-step="3"]'));
  const stage = sectionOf(waveCard(2).querySelector('[data-stage="3a"]'));
  expect(step.findHeaderText().getElement().textContent).toBe('Step 3: DocumentDB');
  expect(stage.findHeaderText().getElement().textContent).toBe('3a: The whole database at once');
  [wave2, step, stage].forEach((section) => {
    expect(isOpen(section)).toBe(false);
    const button = section.findExpandButton();
    expect(button.getElement().getAttribute('aria-controls')).toBeTruthy();
    act(() => button.keyup(13));
    expect(isOpen(section)).toBe(true);
  });
  expect(stage.findContent().getElement().querySelector('[data-figure="stage-gate"]').textContent).toBe('Gate 3a.');
  // A stage's fields are key-value pairs.
  expect(createWrapper(stage.findContent().getElement()).findKeyValuePairs()).not.toBeNull();
  // Each closes again with the keyboard: the stage with Space, the step with Enter, the
  // wave with Space.
  act(() => stage.findExpandButton().keyup(32));
  expect(isOpen(stage)).toBe(false);
  act(() => step.findExpandButton().keyup(13));
  expect(isOpen(step)).toBe(false);
  act(() => wave2.findExpandButton().keyup(32));
  expect(isOpen(wave2)).toBe(false);
});

// ---------------------------------------------------------------------------
// #2287: a gate is a checklist of the facts' conditions
// ---------------------------------------------------------------------------

const LONG = Array.from({ length: 16 }, (_, i) => `wp.t${String(i).padStart(2, '0')}`);

function gateRoadmap() {
  const synthesis = nestedRoadmap();
  synthesis.migration_waves[1].gate = '3 tables (wp.a, wp.b, wp.c) stay. The cutover is your responsibility.';
  synthesis.facts.waves = [{
    ...SUGGESTED,
    wave: 2,
    served_tables: 224,
    gate_conditions: [
      { kind: 'check', parts: [{ text: '3 tables', items: ['wp.a', 'wp.b', 'wp.c'] }, { text: ' stay.', items: [] }] },
      { kind: 'note', parts: [{ text: 'The cutover is your responsibility.', items: [] }] },
    ],
  }];
  const stage3a = synthesis.facts.slice_plan.plan_steps[2].step_stages[0];
  stage3a.gate = `Backfill complete for 16 tables (${LONG.join(', ')}); no other engine reads them.`;
  stage3a.gate_conditions = [{
    kind: 'check',
    parts: [{ text: 'Backfill complete for 16 tables', items: LONG }, { text: '; no other engine reads them.', items: [] }],
  }];
  return synthesis;
}

test('a wave\'s gate is a checklist: a "To check" status per condition, a note without one', () => {
  renderRoadmap(gateRoadmap());
  const section = sectionOf(waveCard(2));
  act(() => section.findExpandButton().keyup(13));
  const gate = section.findContent().getElement().querySelector('ul[data-figure="wave-gate"]');
  expect(gate.getAttribute('aria-label')).toBe('Gate before the next wave');
  const [check, note] = [...gate.children].filter(el => el.tagName === 'LI');
  expect(createWrapper(check).findStatusIndicator().getElement().textContent).toBe('3 tables stay.');
  expect(check.querySelector('[aria-label="To check"]')).not.toBeNull();
  expect(createWrapper(note).findStatusIndicator()).toBeNull();
  expect(note.textContent).toBe('The cutover is your responsibility.');
  // The readiness status is stated once: in the header, not again in the body or the gate.
  expect(createWrapper(section.findContent().getElement().querySelector('[data-figure="wave-readiness"]')).findStatusIndicator()).toBeNull();
});

test('a short table list opens in place with the keyboard, its names a real list and no heading', () => {
  renderRoadmap(gateRoadmap());
  const section = sectionOf(waveCard(2));
  act(() => section.findExpandButton().keyup(13));
  const toggle = section.findContent().getElement().querySelector('[data-figure="gate-names-toggle"]');
  const list = sectionOf(toggle);
  expect(list.findHeaderText().getElement().textContent).toBe('Show 3 tables');
  expect(isOpen(list)).toBe(false);
  act(() => list.findExpandButton().keyup(13));
  expect(isOpen(list)).toBe(true);
  const content = list.findContent().getElement();
  expect([...content.querySelectorAll('li')].map(li => li.textContent)).toEqual(['wp.a', 'wp.b', 'wp.c']);
  expect(content.querySelector('ul').getAttribute('aria-label')).toBe('3 tables');
  // No popover and no heading of its own: the wave's heading order stays whole.
  expect(createWrapper(toggle).findPopover()).toBeNull();
  expect(content.querySelector('h1, h2, h3, h4, h5, h6')).toBeNull();
  expect(createWrapper(content).findTextFilter()).toBeNull();
  act(() => list.findExpandButton().keyup(32));
  expect(isOpen(list)).toBe(false);
});

test('a stage gate\'s long list opens in place with the keyboard, with a filter on the name', () => {
  renderRoadmap(gateRoadmap());
  const wave2 = sectionOf(waveCard(2));
  const step = sectionOf(waveCard(2).querySelector('[data-plan-step="3"]'));
  const stage = sectionOf(waveCard(2).querySelector('[data-stage="3a"]'));
  [wave2, step, stage].forEach(s => act(() => s.findExpandButton().keyup(13)));
  const gate = stage.findContent().getElement().querySelector('ul[data-figure="stage-gate"]');
  expect(gate.getAttribute('aria-label')).toBe('Gate of stage 3a');
  expect(createWrapper(gate).findStatusIndicator().getElement().textContent).toBe('Backfill complete for 16 tables; no other engine reads them.');
  expect(createWrapper(gate).findPopover()).toBeNull();
  const list = sectionOf(gate.querySelector('[data-figure="gate-names-toggle"]'));
  expect(list.findHeaderText().getElement().textContent).toBe('Show 16 tables');
  expect(isOpen(list)).toBe(false);
  act(() => list.findExpandButton().keyup(32));
  expect(isOpen(list)).toBe(true);
  const names = () => [...list.findContent().getElement().querySelectorAll('[data-figure="gate-name"]')].map(li => li.textContent);
  expect(names()).toEqual(LONG);
  const filter = createWrapper(list.findContent().getElement()).findTextFilter();
  expect(filter.findInput().findNativeInput().getElement().getAttribute('aria-label')).toBe('Find a table in this list');
  act(() => filter.findInput().setInputValue('t1'));
  expect(names()).toEqual(LONG.filter(n => n.includes('t1')));
  expect(filter.findResultsCount().getElement().textContent).toBe('6 matches');
  act(() => list.findExpandButton().keyup(13));
  expect(isOpen(list)).toBe(false);
});
