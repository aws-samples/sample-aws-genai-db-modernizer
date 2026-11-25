// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #957: the results page's migration roadmap. It shows report.json's waves (the
 * same ones the executive report and the engineering report show) with each
 * wave's headline, rationale, gate and tables, and computes nothing itself
 * (ADR-032). Cloudscape components are plain DOM stand-ins (see
 * QueryRoutingTable.test.js); the component and the English strings are real.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { createInstance } from 'i18next';
import en from '../../locales/en.json';
import fixture from '../../utils/__tests__/fixtures/waveHeadlines.json';
import MigrationWavePlan from '../MigrationWavePlan';

jest.mock('@cloudscape-design/components/container', () => ({
  __esModule: true,
  default: ({ header, children }) => <section>{header}{children}</section>,
}));
jest.mock('@cloudscape-design/components/header', () => ({
  __esModule: true,
  default: ({ children, description, counter }) => (
    <header><h2>{children}{counter ? ` ${counter}` : ''}</h2><p className="mock-description">{description}</p></header>
  ),
}));
jest.mock('@cloudscape-design/components/box', () => ({
  __esModule: true,
  default: ({ children }) => <div>{children}</div>,
}));
jest.mock('@cloudscape-design/components/space-between', () => ({
  __esModule: true,
  default: ({ children }) => <div>{children}</div>,
}));
jest.mock('@cloudscape-design/components/badge', () => ({
  __esModule: true,
  default: ({ children }) => <span className="mock-badge">{children}</span>,
}));
jest.mock('@cloudscape-design/components/expandable-section', () => ({
  __esModule: true,
  // #2286: a wave, a step and a stage are sections too; their header's description and
  // secondary content (the engines, the headline, the readiness) follow the summary.
  default: ({ headerText, headerDescription, headerSecondaryContent, variant, children }) => (
    <details data-variant={variant}>
      <summary>{headerText}</summary>
      {headerDescription && <p className="mock-section-description">{headerDescription}</p>}
      {headerSecondaryContent}
      {children}
    </details>
  ),
}));

// #2287: the gate checklist's components, as plain DOM stand-ins: a status is its type
// and icon label, a filter its input and count.
jest.mock('@cloudscape-design/components/status-indicator', () => ({
  __esModule: true,
  default: ({ type, iconAriaLabel, children }) => <span data-status-type={type} data-icon-label={iconAriaLabel}>{children}</span>,
}));
jest.mock('@cloudscape-design/components/text-filter', () => ({
  __esModule: true,
  default: ({ filteringText, filteringAriaLabel, countText, onChange }) => (
    <span>
      <input aria-label={filteringAriaLabel} value={filteringText} onChange={e => onChange({ detail: { filteringText: e.target.value } })} />
      <span className="mock-count">{countText}</span>
    </span>
  ),
}));

// #1132: the adoption paths panel's components (not rendered by a report without
// facts.adoption, but imported).
jest.mock('@cloudscape-design/components/column-layout', () => ({
  __esModule: true,
  default: ({ children }) => <div>{children}</div>,
}));
jest.mock('@cloudscape-design/components/toggle-button', () => ({
  __esModule: true,
  default: ({ children }) => <button type="button">{children}</button>,
}));
jest.mock('@cloudscape-design/components/button', () => ({
  __esModule: true,
  default: ({ children }) => <button type="button">{children}</button>,
}));
jest.mock('@cloudscape-design/components/link', () => ({
  __esModule: true,
  default: ({ children, href }) => <a href={href}>{children}</a>,
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

function render(synthesis) {
  act(() => {
    root.render(<MigrationWavePlan synthesis={synthesis} t={t} />);
  });
}

test('renders one card per wave, in report order, with the reports\' headline', () => {
  render(fixture.synthesis);
  const cards = [...container.querySelectorAll('[data-wave]')];
  expect(cards.map(c => c.getAttribute('data-wave'))).toEqual(['1', '2', '3', '4', '5']);
  cards.forEach((card) => {
    const n = card.getAttribute('data-wave');
    expect(card.querySelector('[data-figure="wave-stats"]').textContent).toBe(fixture.expected_headlines[n]);
  });
});

test('the heading states the facts layer\'s wave count and the one-suggested-path note', () => {
  render(fixture.synthesis);
  expect(container.querySelector('h2').textContent).toBe('Migration roadmap (5 waves)');
  expect(container.querySelector('.mock-description').textContent).toBe(
    'This is one suggested adoption path. To modernize in one step instead, '
    + 'adopt the target architecture in Executive summary above directly. '
    + 'The cutover is your responsibility.',
  );
});

// #2107: the cutover is the customer's, said once next to the plan, as both reports do.
test('the plan states once that the cutover is the customer\'s', () => {
  render(fixture.synthesis);
  const lines = container.querySelectorAll('[data-figure="cutover-responsibility"]');
  expect(lines).toHaveLength(1);
  expect(lines[0].textContent).toBe('The cutover is your responsibility.');
});

test('the cutover line prints the facts layer\'s words', () => {
  render({ ...fixture.synthesis, facts: { ...fixture.synthesis.facts, cutover_responsibility: 'Yours to run.' } });
  expect(container.querySelector('[data-figure="cutover-responsibility"]').textContent).toBe('Yours to run.');
});

test('each card shows the wave title, its engines, rationale, gate and tables', () => {
  render(fixture.synthesis);
  const first = container.querySelector('[data-wave="1"]');
  expect(first.textContent).toContain('Wave 1: Move to Aurora MySQL');
  expect([...first.querySelectorAll('[data-engine]')].map(e => e.getAttribute('data-engine'))).toEqual(['aurora_mysql']);
  expect(first.querySelector('[data-engine]').textContent).toBe('Aurora MySQL');
  expect(first.textContent).toContain('Whole schema first.');
  expect(first.querySelector('[data-figure="wave-gate-block"]').textContent).toBe('Gate before the next waveParity validated.');
  expect(conditionTexts(first)).toEqual(['Parity validated.']);
  expect(names(first.querySelector('[data-figure="wave-tables"]'))).toEqual(['wp.a', 'wp.b', 'wp.v1']);
});

test('a wave with no gate or no tables leaves that line out', () => {
  render(fixture.synthesis);
  const cache = container.querySelector('[data-wave="2"]');
  expect(cache.querySelector('[data-figure="wave-tables"]')).toBeNull();
  const search = container.querySelector('[data-wave="4"]');
  expect(search.querySelector('[data-figure="wave-gate"]')).toBeNull();
});

test('a wave with no title is named by its engines, like the reports', () => {
  const wave = { wave: 1, engines: ['dynamodb', 'documentdb'], query_count: 2, workload_share_percent: 5, table_count: 0 };
  render({ migration_waves: [wave], facts: { waves: [], migration_wave_count: 1 } });
  expect(container.querySelector('[data-wave="1"]').textContent).toContain('Wave 1: DynamoDB, DocumentDB');
  expect(container.querySelector('h2').textContent).toBe('Migration roadmap (1 wave)');
});

test('renders nothing when the report has no waves', () => {
  render({ migration_waves: [], facts: { waves: [], migration_wave_count: 0 } });
  expect(container.innerHTML).toBe('');
  render({});
  expect(container.innerHTML).toBe('');
});

test('report text is rendered as text, never as markup', () => {
  const hostile = '<img src=x onerror=alert(1)><script>alert(1)</script>';
  const wave = {
    wave: 1, title: hostile, engines: ['dynamodb'], query_count: 1, workload_share_percent: 1,
    table_count: 1, tables: [hostile], rationale: hostile, gate: hostile,
  };
  render({ migration_waves: [wave], facts: { waves: [], migration_wave_count: 1 } });
  expect(container.querySelector('img')).toBeNull();
  expect(container.querySelector('script')).toBeNull();
  expect(container.textContent).toContain(hostile);
});

// #2287: a gate is a checklist of the conditions the facts cut it into
// (facts.waves[].gate_conditions), each table list behind "Show N tables"; the UI never
// parses the gate's prose. The conditions joined back are report.json's gate.
const GATE = 'Dual-write validated. 3 tables (wp.options, wp.termmeta, wp.keys) stay dual-read by Aurora MySQL queries. '
  + '2 queries read 2 tables (wp.posts, wp.users) Aurora MySQL owns (wave 1) and keeps owning. '
  + 'The cutover is your responsibility.';
const GATE_PARTS = [
  { text: 'Dual-write validated. 3 tables', items: ['wp.options', 'wp.termmeta', 'wp.keys'] },
  { text: ' stay dual-read by Aurora MySQL queries. 2 queries read 2 tables', items: ['wp.posts', 'wp.users'] },
  { text: ' Aurora MySQL owns (wave 1) and keeps owning. The cutover is your responsibility.', items: [] },
];
// As src/shared/gate_conditions.py cuts GATE_PARTS (tests/unit/shared/test_gate_conditions.py).
const CONDITIONS = [
  { kind: 'check', parts: [{ text: 'Dual-write validated.', items: [] }] },
  { kind: 'check', parts: [{ text: '3 tables', items: ['wp.options', 'wp.termmeta', 'wp.keys'] }, { text: ' stay dual-read by Aurora MySQL queries.', items: [] }] },
  { kind: 'check', parts: [{ text: '2 queries read 2 tables', items: ['wp.posts', 'wp.users'] }, { text: ' Aurora MySQL owns (wave 1) and keeps owning.', items: [] }] },
  { kind: 'note', parts: [{ text: 'The cutover is your responsibility.', items: [] }] },
];
const listWave = {
  wave: 3, title: 'Move to DynamoDB', engines: ['dynamodb'], query_count: 37, workload_share_percent: 34.6,
  table_count: 2, tables: ['wp.posts', 'wp.users'], rationale: 'Key-value reads.', gate: GATE, gate_parts: GATE_PARTS,
};
const listSynthesis = (conditions = CONDITIONS, wave = listWave) => ({
  migration_waves: [wave],
  facts: { waves: [{ wave: wave.wave, gate_conditions: conditions }], migration_wave_count: 1 },
});
const conditionTexts = el => [...el.querySelectorAll('[data-figure="gate-condition-text"]')].map(n => n.textContent);
const names = el => [...el.querySelectorAll('[data-figure="gate-name"]')].map(n => n.textContent);
const type = (input, value) => act(() => {
  Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set.call(input, value);
  input.dispatchEvent(new Event('input', { bubbles: true }));
});

test('a gate is a checklist, one item per condition in the facts\' order, under its heading', () => {
  render(listSynthesis());
  const block = container.querySelector('[data-figure="wave-gate-block"]');
  expect(block.firstChild.textContent).toMatch(/^Gate before the next wave/);
  const list = block.querySelector('ul[data-figure="wave-gate"]');
  expect(list.getAttribute('aria-label')).toBe('Gate before the next wave');
  const items = [...list.children];
  expect(items.map(li => li.tagName)).toEqual(['LI', 'LI', 'LI', 'LI']);
  expect(conditionTexts(list)).toEqual([
    'Dual-write validated.',
    '3 tables stay dual-read by Aurora MySQL queries.',
    '2 queries read 2 tables Aurora MySQL owns (wave 1) and keeps owning.',
    'The cutover is your responsibility.',
  ]);
  expect(items.map(li => li.getAttribute('data-kind'))).toEqual(['check', 'check', 'check', 'note']);
});

test('each condition to check has a "To check" status; a note has none', () => {
  render(listSynthesis());
  const items = [...container.querySelectorAll('[data-figure="gate-condition"]')];
  items.slice(0, 3).forEach((li) => {
    const status = li.querySelector('[data-status-type]');
    expect(status.getAttribute('data-status-type')).toBe('not-started');
    expect(status.getAttribute('data-icon-label')).toBe('To check');
  });
  expect(items[3].querySelector('[data-status-type]')).toBeNull();
});

test('each list a condition names is behind "Show N tables", a real list of the facts\' names', () => {
  render(listSynthesis());
  const items = [...container.querySelectorAll('[data-figure="gate-condition"]')];
  expect(items[0].querySelector('[data-figure="gate-names-toggle"]')).toBeNull();
  const three = items[1].querySelector('[data-figure="gate-names-toggle"]');
  expect(three.querySelector('summary').textContent).toBe('Show 3 tables');
  const list = three.querySelector('ul');
  expect(list.getAttribute('aria-label')).toBe('3 tables');
  // A short list opens with no filter and never scrolls, so it adds no tab stop.
  expect(three.querySelector('input')).toBeNull();
  expect(list.getAttribute('tabindex')).toBeNull();
  expect(names(three)).toEqual(['wp.options', 'wp.termmeta', 'wp.keys']);
  expect(items[2].querySelector('[data-figure="gate-names-toggle"] summary').textContent).toBe('Show 2 tables');
  // No name of a list is in the sentence itself.
  expect(conditionTexts(container).join(' ')).not.toContain('wp.');
});

test('one table reads "Show 1 table"', () => {
  render(listSynthesis([{ kind: 'check', parts: [{ text: '1 query reads 1 table', items: ['wp.users'] }] }]));
  expect(container.querySelector('[data-figure="gate-names-toggle"] summary').textContent).toBe('Show 1 table');
});

const LONG = Array.from({ length: 16 }, (_, i) => `wp.t${String(i).padStart(2, '0')}`);

test('a list of more than 15 tables opens with a filter on the name', () => {
  render(listSynthesis([{ kind: 'check', parts: [{ text: '16 tables', items: LONG }, { text: ' move.', items: [] }] }]));
  const toggle = container.querySelector('[data-figure="gate-names-toggle"]');
  expect(toggle.querySelector('summary').textContent).toBe('Show 16 tables');
  expect(names(toggle)).toEqual(LONG);
  // A list that scrolls takes keyboard focus (axe scrollable-region-focusable).
  expect(toggle.querySelector('ul').getAttribute('tabindex')).toBe('0');
  const input = toggle.querySelector('input');
  expect(input.getAttribute('aria-label')).toBe('Find a table in this list');
  type(input, 't1');
  expect(names(toggle)).toEqual(LONG.filter(n => n.includes('t1')));
  expect(toggle.querySelector('.mock-count').textContent).toBe('6 matches');
  type(input, 'nothing');
  expect(names(toggle)).toEqual([]);
  expect(toggle.textContent).toContain('No table matches.');
});

test('a list of 15 tables opens with no filter and no scroll box', () => {
  render(listSynthesis([{ kind: 'check', parts: [{ text: '15 tables', items: LONG.slice(0, 15) }] }]));
  const toggle = container.querySelector('[data-figure="gate-names-toggle"]');
  expect(names(toggle)).toEqual(LONG.slice(0, 15));
  expect(toggle.querySelector('input')).toBeNull();
  expect(toggle.querySelector('ul').getAttribute('tabindex')).toBeNull();
});

test('a report whose facts have no conditions shows its gate string as one condition, unchanged', () => {
  render({ migration_waves: [listWave], facts: { waves: [], migration_wave_count: 1 } });
  expect(conditionTexts(container)).toEqual([GATE]);
  expect(container.querySelector('[data-figure="gate-names-toggle"]')).toBeNull();
});

test('facts that state no condition show the gate as one condition, as the engineering report does', () => {
  render(listSynthesis([]));
  expect(conditionTexts(container)).toEqual([GATE]);
  render(listSynthesis([], { ...listWave, gate: '', gate_parts: [] }));
  expect(container.querySelector('[data-figure="wave-gate-block"]')).toBeNull();
});

test('the Tables expander stays at the end of the card, a list with a filter on the name', () => {
  render(listSynthesis());
  const card = container.querySelector('[data-wave="3"]');
  const gate = card.querySelector('[data-figure="wave-gate"]');
  const tables = card.querySelector('details[data-variant="footer"]');
  expect(gate.compareDocumentPosition(tables) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(tables.querySelector('summary').textContent).toBe('Tables');
  const list = tables.querySelector('ul[data-figure="wave-tables"]');
  expect(list.getAttribute('aria-label')).toBe('Tables of wave 3');
  // A short list never scrolls, so it adds no tab stop.
  expect(list.getAttribute('tabindex')).toBeNull();
  expect(names(list)).toEqual(['wp.posts', 'wp.users']);
  type(tables.querySelector('input'), 'users');
  expect(names(tables)).toEqual(['wp.users']);
});

test('a hostile list item is text, never markup', () => {
  const hostile = '<img src=x onerror=alert(1)>';
  render(listSynthesis([{ kind: 'check', parts: [{ text: '1 table', items: [hostile] }] }]));
  expect(names(container.querySelector('[data-figure="gate-names-toggle"]'))).toEqual([hostile]);
  expect(container.querySelector('img')).toBeNull();
  expect(container.textContent).toContain(hostile);
});

test('a saved report written before gate_parts (the shared fixture) renders every gate unchanged', () => {
  const waves = fixture.synthesis.migration_waves;
  expect(waves.some(w => w.gate)).toBe(true);
  expect(waves.every(w => w.gate_parts === undefined)).toBe(true);
  render(fixture.synthesis);
  waves.filter(w => w.gate).forEach((w) => {
    const gate = container.querySelector(`[data-wave="${w.wave}"] [data-figure="wave-gate"]`);
    expect(conditionTexts(gate)).toEqual([w.gate]);
    expect(gate.querySelector('button')).toBeNull();
  });
});

// #1120: a cross-engine wave 1 states the facts layer's note and lists the detected
// relational gaps (facts.relational_gaps) under it, as the reports do.
const NOTE = 'Wave 1 is not yet optimized for heterogeneous migrations: it converts all 5 source '
  + 'tables from SQL Server to Aurora PostgreSQL with their current data model.';
const heterogeneous = {
  migration_waves: [
    { wave: 1, title: 'Move off SQL Server to Aurora PostgreSQL (cross-engine)', engines: ['aurora_postgresql'], query_count: 4, workload_share_percent: 40, table_count: 5 },
    { wave: 2, title: 'Move to DynamoDB', engines: ['dynamodb'], query_count: 6, workload_share_percent: 60, table_count: 3 },
  ],
  facts: {
    migration_wave_count: 2,
    waves: [
      { wave: 1, served_tables: 5, heterogeneous_note: NOTE, lists_relational_gaps: true },
      { wave: 2, served_tables: 3, heterogeneous_note: null },
    ],
    relational_gaps: {
      gaps: [
        { rule_id: 'sqlserver-top-clause', feature: 'TOP row limits', impact: 'needs_rewrite', reason: 'TOP (n) becomes LIMIT n.' },
        { rule_id: 'sqlserver-babelfish-option', feature: 'Babelfish for Aurora PostgreSQL', impact: 'informational', reason: 'Babelfish accepts T-SQL.' },
      ],
    },
  },
};

test('a cross-engine wave 1 shows the not-yet-optimized note and every relational gap', () => {
  render(heterogeneous);
  const first = container.querySelector('[data-wave="1"]');
  expect(first.querySelector('[data-figure="wave-heterogeneous-note"]').textContent).toBe(NOTE);
  expect([...first.querySelectorAll('[data-figure="wave-gap"]')].map(g => g.textContent)).toEqual([
    'TOP row limits (needs a rewrite): TOP (n) becomes LIMIT n.',
    'Babelfish for Aurora PostgreSQL (informational): Babelfish accepts T-SQL.',
  ]);
  const second = container.querySelector('[data-wave="2"]');
  expect(second.querySelector('[data-figure="wave-heterogeneous-note"]')).toBeNull();
  expect(second.querySelector('[data-figure="wave-gap"]')).toBeNull();
});

test('a wave 1 with no note lists no gaps, even when the gap check found some', () => {
  const homogeneous = {
    ...heterogeneous,
    facts: { ...heterogeneous.facts, waves: heterogeneous.facts.waves.map(w => ({ ...w, heterogeneous_note: null })) },
  };
  render(homogeneous);
  expect(container.querySelector('[data-figure="wave-heterogeneous-note"]')).toBeNull();
  expect(container.querySelector('[data-figure="wave-gap"]')).toBeNull();
});

test('a gap check for another engine is not listed under wave 1', () => {
  const other = {
    ...heterogeneous,
    facts: { ...heterogeneous.facts, waves: heterogeneous.facts.waves.map(w => ({ ...w, lists_relational_gaps: false })) },
  };
  render(other);
  expect(container.querySelector('[data-figure="wave-heterogeneous-note"]').textContent).toBe(NOTE);
  expect(container.querySelector('[data-figure="wave-gap"]')).toBeNull();
});

test('a wave that moves nothing shows no stats line and no tables section (#1383)', () => {
  const wave = {
    wave: 1, title: 'No move to Aurora', engines: [], query_count: 0, workload_share_percent: 0,
    table_count: 50, tables: ['wp.a', 'wp.b'], gate: 'No Aurora move is planned.',
  };
  render({
    migration_waves: [wave],
    facts: { waves: [{ wave: 1, no_move: true, served_tables: 50 }], migration_wave_count: 1 },
  });
  const card = container.querySelector('[data-wave="1"]');
  expect(card.textContent).toContain('No move to Aurora');
  expect(card.querySelector('[data-figure="wave-stats"]')).toBeNull();
  expect(card.querySelector('[data-figure="wave-tables"]')).toBeNull();
  expect(card.querySelector('[data-engine]')).toBeNull();
});

// #1992: each wave's readiness, as the facts state it (facts.waves[].readiness_line),
// with the rule once; a report written before the check shows neither.
function withReadiness(synthesis) {
  const out = JSON.parse(JSON.stringify(synthesis));
  out.facts.waves = out.facts.waves.map(w => (w.wave === 3
    ? { ...w, readiness: 'held', suggested: false, readiness_line: 'Not suggested yet: an open high risk on DynamoDB (RISK-001, performance degradation). Next step: run the DynamoDB schema design, then check the wave again.' }
    : { ...w, readiness: 'suggested', suggested: true, readiness_line: 'Suggested: no check holds it.' }));
  return out;
}

test('each card states the wave\'s readiness, and the rule is in the help panel (#2512), not the section', () => {
  render(withReadiness(fixture.synthesis));
  const held = container.querySelector('[data-wave="3"] [data-figure="wave-readiness"]');
  // #2318, #2287: the facts' line under the label; the status ("Not suggested yet") is
  // stated once, in the wave's header.
  expect(held.textContent).toBe(
    'Readiness'
    + 'Not suggested yet: an open high risk on DynamoDB (RISK-001, performance '
    + 'degradation). Next step: run the DynamoDB schema design, then check the wave again.',
  );
  expect(held.getAttribute('data-readiness')).toBe('held');
  expect(held.querySelector('[data-status-type]')).toBeNull();
  const header = container.querySelector('[data-wave="3"] [data-figure="wave-header-readiness"]');
  expect(header.querySelector('[data-status-type]').textContent).toBe('Not suggested yet');
  expect(container.querySelectorAll('[data-wave="3"] [data-figure="wave-header-readiness"]')).toHaveLength(1);
  const one = container.querySelector('[data-wave="1"] [data-figure="wave-readiness"]');
  expect(one.textContent).toBe('ReadinessSuggested: no check holds it.');
  expect(container.querySelectorAll('[data-figure="wave-readiness-rule"]')).toHaveLength(0);
  expect(container.textContent).not.toContain(en['analysis-results.waves.readiness-rule']);
});

test('a report written before the readiness check shows no readiness', () => {
  render(fixture.synthesis);
  expect(container.querySelectorAll('[data-figure="wave-readiness"]')).toHaveLength(0);
  expect(container.querySelectorAll('[data-figure="wave-readiness-rule"]')).toHaveLength(0);
});
