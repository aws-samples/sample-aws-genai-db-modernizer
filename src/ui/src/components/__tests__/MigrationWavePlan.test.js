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
  default: ({ headerText, children }) => <details><summary>{headerText}</summary>{children}</details>,
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
    + 'adopt the target architecture in Executive summary above directly.',
  );
});

test('each card shows the wave title, its engines, rationale, gate and tables', () => {
  render(fixture.synthesis);
  const first = container.querySelector('[data-wave="1"]');
  expect(first.textContent).toContain('Wave 1: Move to Aurora MySQL');
  expect([...first.querySelectorAll('[data-engine]')].map(e => e.getAttribute('data-engine'))).toEqual(['aurora_mysql']);
  expect(first.querySelector('[data-engine]').textContent).toBe('Aurora MySQL');
  expect(first.textContent).toContain('Whole schema first.');
  expect(first.querySelector('[data-figure="wave-gate"]').textContent).toBe('Gate before the next wave: Parity validated.');
  expect(first.querySelector('[data-figure="wave-tables"]').textContent).toBe('wp.a, wp.b, wp.v1');
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

// #1028: a gate's long lists collapse to an inline "(…)" that expands in
// place. The lists come from report.json's gate_parts (the producer splits
// them out); the UI never parses the gate's prose.
const GATE = 'Dual-write validated. 3 tables (wp.options, wp.termmeta, wp.keys) stay dual-read by Aurora MySQL queries. '
  + '2 queries read 2 tables (wp.posts, wp.users) Aurora MySQL owns (wave 1) and keeps owning.';
const GATE_PARTS = [
  { text: 'Dual-write validated. 3 tables', items: ['wp.options', 'wp.termmeta', 'wp.keys'] },
  { text: ' stay dual-read by Aurora MySQL queries. 2 queries read 2 tables', items: ['wp.posts', 'wp.users'] },
  { text: ' Aurora MySQL owns (wave 1) and keeps owning.', items: [] },
];
const listWave = {
  wave: 3, title: 'Move to DynamoDB', engines: ['dynamodb'], query_count: 37, workload_share_percent: 34.6,
  table_count: 2, tables: ['wp.posts', 'wp.users'], rationale: 'Key-value reads.', gate: GATE, gate_parts: GATE_PARTS,
};

test('a gate shows each list as an inline (…) by default, the count and noun kept', () => {
  render({ migration_waves: [listWave], facts: { waves: [], migration_wave_count: 1 } });
  const gate = container.querySelector('[data-figure="wave-gate"]');
  expect(gate.textContent).toBe(
    'Gate before the next wave: Dual-write validated. 3 tables (…) stay dual-read by Aurora MySQL queries. '
    + '2 queries read 2 tables (…) Aurora MySQL owns (wave 1) and keeps owning.',
  );
  const toggles = [...gate.querySelectorAll('button[aria-expanded]')];
  expect(toggles.map(b => b.getAttribute('aria-expanded'))).toEqual(['false', 'false']);
  expect(toggles.map(b => b.getAttribute('aria-label'))).toEqual(['Show the 3 names', 'Show the 2 names']);
});

// The gate as a reader sees it, without the small "hide" controls.
const readGate = (el) => {
  const copy = el.cloneNode(true);
  copy.querySelectorAll('[data-gate-hide]').forEach(n => n.remove());
  return copy.textContent;
};

test('clicking a (…) expands that list in place; expanded, the gate reads as report.json\'s gate', () => {
  render({ migration_waves: [listWave], facts: { waves: [], migration_wave_count: 1 } });
  const gate = container.querySelector('[data-figure="wave-gate"]');
  act(() => gate.querySelectorAll('button[aria-expanded="false"]')[0].click());
  expect(gate.querySelector('[data-figure="gate-list"]').textContent).toBe('(wp.options, wp.termmeta, wp.keys)');
  expect(gate.querySelectorAll('button[aria-expanded="false"]')).toHaveLength(1);
  act(() => gate.querySelector('button[aria-expanded="false"]').click());
  expect(readGate(gate)).toBe(`Gate before the next wave: ${GATE}`);
  const hide = gate.querySelectorAll('[data-gate-hide]')[0];
  act(() => hide.click());
  expect(gate.querySelectorAll('[data-figure="gate-list"]')).toHaveLength(1);
  expect(gate.textContent).toContain('3 tables (…)');
});

test('an open list is text outside its control, so a screen reader reads the names', () => {
  render({ migration_waves: [listWave], facts: { waves: [], migration_wave_count: 1 } });
  const gate = container.querySelector('[data-figure="wave-gate"]');
  act(() => gate.querySelector('button[aria-expanded="false"]').click());
  const list = gate.querySelector('[data-figure="gate-list"]');
  expect(list.closest('button')).toBeNull();
  expect(list.hasAttribute('aria-hidden')).toBe(false);
  const hide = gate.querySelector('[data-gate-hide]');
  expect(hide.tagName).toBe('BUTTON');
  expect(hide.getAttribute('aria-expanded')).toBe('true');
  expect(hide.getAttribute('aria-controls')).toBe(list.id);
  expect(hide.getAttribute('aria-label')).toBe('Hide the 3 names');
});

test('focus moves to the control that is shown: hide once open, show once closed', () => {
  render({ migration_waves: [listWave], facts: { waves: [], migration_wave_count: 1 } });
  const gate = container.querySelector('[data-figure="wave-gate"]');
  const show = gate.querySelector('button[aria-expanded="false"]');
  show.focus();
  act(() => show.click());
  expect(document.activeElement).toBe(gate.querySelector('[data-gate-hide]'));
  act(() => document.activeElement.click());
  expect(document.activeElement.getAttribute('aria-label')).toBe('Show the 3 names');
  expect(document.activeElement.getAttribute('aria-expanded')).toBe('false');
});

test('a report written before gate_parts shows its gate string as it is', () => {
  const legacy = { ...listWave, gate_parts: undefined };
  render({ migration_waves: [legacy], facts: { waves: [], migration_wave_count: 1 } });
  const gate = container.querySelector('[data-figure="wave-gate"]');
  expect(gate.textContent).toBe(`Gate before the next wave: ${GATE}`);
  expect(gate.querySelector('button')).toBeNull();
});

test('the Tables expander stays at the end of the card', () => {
  render({ migration_waves: [listWave], facts: { waves: [], migration_wave_count: 1 } });
  const card = container.querySelector('[data-wave="3"]');
  const gate = card.querySelector('[data-figure="wave-gate"]');
  const tables = card.querySelector('details');
  expect(gate.compareDocumentPosition(tables) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(tables.querySelector('summary').textContent).toBe('Tables');
});

test('a hostile list item is text, never markup', () => {
  const hostile = '<img src=x onerror=alert(1)>';
  const wave = { ...listWave, gate: `1 table (${hostile})`, gate_parts: [{ text: '1 table', items: [hostile] }] };
  render({ migration_waves: [wave], facts: { waves: [], migration_wave_count: 1 } });
  act(() => container.querySelector('[data-figure="wave-gate"] button').click());
  expect(container.querySelector('[data-figure="gate-list"]').textContent).toBe(`(${hostile})`);
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
    expect(gate.textContent).toBe(`Gate before the next wave: ${w.gate}`);
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
