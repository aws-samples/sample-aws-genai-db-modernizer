/**
 * #339: the query journey's "Why this engine" section renders the decision
 * trace from `GET .../graph/queries/{query_id}/trace` (QueryTraceResponse).
 *
 * The main fixture is a real trace: the wordpress sample's REPLACE INTO
 * wp_wc_product_meta_lookup query (deterministic pipeline), which its first
 * assignment put on DocumentDB (77) and the reality check moved to DynamoDB
 * (69) when it consolidated DocumentDB away. Cloudscape ships ESM that this
 * repo's Jest config cannot transform (see AnalysisResultsSummaryFallback.test.js),
 * so Box/SpaceBetween/StatusIndicator/Badge are plain DOM stand-ins; the
 * component itself is real, and so are the English strings (src/locales/en.json).
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { createInstance } from 'i18next';
import en from '../../locales/en.json';
import QueryDecisionTrace, { formatTraceScore } from '../QueryDecisionTrace';
import movedTrace from './fixtures/wordpressMovedQueryTrace.json';

jest.mock('@cloudscape-design/components/box', () => ({
  __esModule: true,
  default: ({ children, 'data-testid': testId }) => <div data-testid={testId}>{children}</div>,
}));
jest.mock('@cloudscape-design/components/space-between', () => ({
  __esModule: true,
  default: ({ children }) => <div>{children}</div>,
}));
jest.mock('@cloudscape-design/components/status-indicator', () => ({
  __esModule: true,
  default: ({ type, children }) => <span data-status={type}>{children}</span>,
}));
jest.mock('@cloudscape-design/components/badge', () => ({
  __esModule: true,
  default: ({ children }) => <span className="mock-badge">{children}</span>,
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

const render = (props) => {
  act(() => root.render(<QueryDecisionTrace t={t} {...props} />));
};
const section = (id) => container.querySelector(`[data-testid="${id}"]`);
const text = (id) => (section(id) ? section(id).textContent : null);

describe('a query the reality check moved', () => {
  beforeEach(() => render({ trace: movedTrace }));

  test('the first assignment shows every engine with its score or gate', () => {
    const first = section('query-trace-first');
    expect(first.textContent).toContain('First assignment (version 1)');
    expect(first.textContent).toContain('Step 1 · version 1 · highest score');
    expect(first.textContent).toContain('DocumentDB scored highest for this query (77), ahead of DynamoDB (69).');
    const rows = Array.from(first.querySelectorAll('tbody tr')).map((tr) =>
      Array.from(tr.querySelectorAll('td')).map((td) => td.textContent));
    expect(rows).toEqual([
      ['Aurora MySQL', '54', '—'],
      ['DocumentDB', '77', '—'],
      ['DynamoDB', '69', '—'],
      ['ElastiCache', '38', 'cannot own a query that writes data (not a system of record)'],
      ['OpenSearch', '0', 'cannot own a query that writes data (not a system of record)'],
    ]);
  });

  test('each later move shows its version and its reason', () => {
    const items = Array.from(section('query-trace-moves').querySelectorAll('li'));
    expect(items.map((li) => li.getAttribute('data-version'))).toEqual(['2']);
    expect(items[0].textContent).toContain('Step 2 · version 2 · engine consolidated away');
    expect(items[0].textContent).toContain(
      'Moved from DocumentDB to DynamoDB (77 to 69): the reality check removed DocumentDB',
    );
  });

  test('the final engine, its version, and the runner-up with its gap', () => {
    expect(text('query-trace-final')).toContain('DynamoDB (69)');
    expect(text('query-trace-final')).toContain('version 2');
    // final_version equals the effective version: no second badge.
    expect(text('query-trace-final')).not.toContain('effective assignment');
    const runner = text('query-trace-runner-up');
    expect(runner).toContain('Aurora MySQL was next at 54, 15 points behind DynamoDB (69).');
    expect(runner).toContain('15 points behind');
    expect(runner).not.toContain("group's mean scores");
  });

  test('the engine that scored higher names the step that decided against it', () => {
    const items = Array.from(section('query-trace-outscored').querySelectorAll('li'));
    expect(items.map((li) => li.getAttribute('data-engine'))).toEqual(['documentdb']);
    expect(items[0].textContent).toContain('DocumentDB scored higher (77 against 69)');
    expect(items[0].textContent).toContain('Decided at step 2, version 2 (engine consolidated away)');
  });

  test('no open tie is shown when there is none', () => {
    expect(section('query-trace-tie')).toBeNull();
    expect(text('query-trace-summary')).toBe(movedTrace.summary);
  });
});

test('an open tie lists the tied engines', () => {
  render({
    trace: {
      ...movedTrace,
      tied_engines: ['documentdb', 'dynamodb'],
      tied_engine_names: ['DocumentDB', 'DynamoDB'],
      outscored_by: [],
    },
  });
  expect(text('query-trace-tie')).toContain(
    'DocumentDB, DynamoDB tie for this query; no routing rule decides between them, so it is listed under DynamoDB pending your choice.',
  );
});

test('the step number is the query\'s own, never the ledger\'s version-wide seq', () => {
  // The ledger numbers Moves across the whole version: this query's second
  // step is the version's 17th Move.
  const move = { ...movedTrace.moves[0], seq: 17 };
  const outscored = { ...movedTrace.outscored_by[0], decided_by_seq: 17, decided_by_step: 2 };
  render({ trace: { ...movedTrace, moves: [move], outscored_by: [outscored] } });
  expect(text('query-trace-moves')).toContain('Step 2 · version 2');
  expect(text('query-trace-outscored')).toContain('Decided at step 2, version 2 (engine consolidated away)');
  expect(container.textContent).not.toContain('17');
});

test('the candidate table is labelled for assistive technology', () => {
  render({ trace: movedTrace });
  const table = section('query-trace-first').querySelector('table');
  expect(table.getAttribute('aria-label')).toBe("Every engine's score or gate at the first assignment");
  expect(Array.from(table.querySelectorAll('th')).map((th) => th.getAttribute('scope'))).toEqual(['col', 'col', 'col']);
});

test('a group-mean runner-up shows one scale: the group means', () => {
  render({
    trace: {
      ...movedTrace,
      final_group_mean: 80.1,
      effective_version: 3,
      runner_up: {
        engine: 'dynamodb',
        engine_name: 'DynamoDB',
        score: null,
        final_score: 80.1,
        basis: 'group_mean',
        gap: null,
        gate: 'group_owner',
        explanation: 'DynamoDB could not take the query: cannot own every query in its co-dependency group.',
      },
    },
  });
  const runner = text('query-trace-runner-up');
  expect(runner).toContain('cannot own every query in its co-dependency group');
  expect(runner).toContain("the final engine's group mean is 80.1");
  expect(runner).not.toContain('points behind');
  expect(text('query-trace-final')).toContain('effective assignment: version 3');
});

test('a query that never moved says so', () => {
  render({ trace: { ...movedTrace, moves: [], outscored_by: [] } });
  expect(text('query-trace-moves')).toContain('No later routing step changed this query.');
  expect(section('query-trace-outscored')).toBeNull();
});

test('a job written before the move ledger shows only the final engine', () => {
  render({
    trace: {
      query_id: 'q1',
      has_ledger: false,
      first_assignment: null,
      moves: [],
      final_engine: 'dynamodb',
      final_engine_name: 'DynamoDB',
      runner_up: null,
      outscored_by: [],
      tied_engine_names: [],
      summary: 'Ends on DynamoDB; this assessment predates the per-query decision record.',
    },
  });
  expect(text('query-trace-no-ledger')).toBe(
    'This assessment predates the per-query decision record, so only the final engine is known.',
  );
  expect(text('query-trace-final')).toBe('DynamoDB');
  expect(section('query-trace-first')).toBeNull();
  expect(section('query-trace-runner-up')).toBeNull();
});

test('loading, error and empty states', () => {
  render({ loading: true });
  expect(container.querySelector('[data-status="loading"]').textContent).toBe('Loading the decision trace...');
  render({ error: 'Query not found' });
  expect(text('query-trace-error')).toBe('The decision trace could not be loaded: Query not found');
  render({ trace: null });
  expect(container.textContent).toBe('No decision trace is available for this query.');
});

test('trace text is rendered as text, never as markup', () => {
  render({
    trace: {
      ...movedTrace,
      summary: '<img src=x onerror=alert(1)>',
      first_assignment: { ...movedTrace.first_assignment, explanation: '<script>alert(1)</script>' },
    },
  });
  expect(container.querySelector('img')).toBeNull();
  expect(container.querySelector('script')).toBeNull();
  expect(text('query-trace-summary')).toBe('<img src=x onerror=alert(1)>');
});

test('formatTraceScore', () => {
  expect(formatTraceScore(69)).toBe('69');
  expect(formatTraceScore(80.1340206185567)).toBe('80.1');
  expect(formatTraceScore(0)).toBe('0');
  expect(formatTraceScore(null)).toBe('—');
  expect(formatTraceScore(undefined)).toBe('—');
});

test('every key the component uses resolves to an English label', () => {
  // eslint-disable-next-line global-require
  const fs = require('fs');
  // eslint-disable-next-line global-require
  const path = require('path');
  const source = fs.readFileSync(path.join(__dirname, '..', 'QueryDecisionTrace.js'), 'utf8');
  const keys = [...source.matchAll(/\bt\(['"]([^'"]+)['"]/g)].map((m) => m[1]);
  expect(keys.length).toBeGreaterThan(10);
  keys.forEach((key) => expect(t(key, { count: 2 })).not.toBe(key));
});

describe('the keys the redesign considered (#1361)', () => {
  const refused = {
    column: 'user_profiles.granted_title_badge_id',
    table_id: 'discourse.user_profiles',
    via: 'not_null_test',
    outcome: 'refused',
    reasons: ['already_a_key', 'rewritten_by_write', 'no_selectivity_signal'],
    explanation:
      "refused for an IS NOT NULL test (a sparse index), for 3 reasons: (1) already indexed in the source; (2) a column the statement's own write rewrites; (3) an IS NOT NULL test with no selectivity signal",
  };
  const accepted = {
    column: 'orders.email',
    table_id: 'shop.orders',
    via: 'equality',
    outcome: 'accepted',
    reasons: [],
    explanation: 'accepted: a key the redesign may assume for an equality filter',
  };

  test('each candidate shows its outcome and the server sentence naming every guard', () => {
    render({ trace: { ...movedTrace, key_candidates: [refused, accepted] } });
    const items = Array.from(section('query-trace-key-candidates').querySelectorAll('li'));
    expect(items.map((li) => [li.getAttribute('data-column'), li.getAttribute('data-outcome')])).toEqual([
      ['user_profiles.granted_title_badge_id', 'refused'],
      ['orders.email', 'accepted'],
    ]);
    expect(items[0].textContent).toContain('refused');
    expect(items[0].textContent).toContain("(2) a column the statement's own write rewrites");
    expect(items[1].textContent).toContain('accepted: a key the redesign may assume');
    expect(text('query-trace-key-candidates')).toContain('Keys the redesign considered');
  });

  test('a trace with none, or written before them, shows no section', () => {
    render({ trace: movedTrace });
    expect(section('query-trace-key-candidates')).toBeNull();
    render({ trace: { ...movedTrace, key_candidates: [] } });
    expect(section('query-trace-key-candidates')).toBeNull();
  });

  test('a job before the move ledger still shows them', () => {
    render({ trace: { query_id: 'q', has_ledger: false, final_engine_name: 'DynamoDB', key_candidates: [refused] } });
    expect(section('query-trace-key-candidates')).not.toBeNull();
  });
});
