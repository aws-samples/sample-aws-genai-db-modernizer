/**
 * #1028: the executive summary's tiles. The access-pattern (query) count sits
 * just below the database name, and a cost or savings tile whose figure is not
 * available is left out instead of saying so. Cloudscape components are plain
 * DOM stand-ins (see QueryRoutingTable.test.js); the component and the English
 * strings are real.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { createInstance } from 'i18next';
import en from '../../locales/en.json';
import ExecutiveSummaryStats from '../ExecutiveSummaryStats';
import { costBaselineStats } from '../../utils/tcoAnalysis';

jest.mock('@cloudscape-design/components/box', () => ({
  __esModule: true,
  default: ({ children }) => <div>{children}</div>,
}));
jest.mock('@cloudscape-design/components/column-layout', () => ({
  __esModule: true,
  default: ({ children }) => <div className="mock-columns">{children}</div>,
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

const KNOWN = costBaselineStats({ current_monthly_cost: 1000, current_cost_known: true, savings_percent: 41.7 });
const UNKNOWN = costBaselineStats({ current_monthly_cost: 0, current_cost_known: false, savings_percent: 0 });

function render(props) {
  act(() => {
    root.render(
      <ExecutiveSummaryStats
        t={t}
        databaseName="wordpress"
        accessPatternCount={107}
        ownerQueryCount={107}
        cachePatternCount={0}
        targetEngines={<span className="engines">Aurora MySQL</span>}
        projectedCost={582.76}
        costBaseline={KNOWN}
        {...props}
      />,
    );
  });
}

const tiles = () => [...container.querySelectorAll('[data-tile]')].map(el => el.getAttribute('data-tile'));

test('the access-pattern count sits just below the database name, in the database tile', () => {
  render();
  expect(tiles()).toEqual(['database', 'target-engines', 'projected-cost', 'current-cost', 'savings']);
  const db = container.querySelector('[data-tile="database"]');
  expect(db.textContent).toBe('DatabasewordpressAccess patterns107');
  const name = db.querySelector('[data-figure="database-name"]');
  const count = db.querySelector('[data-figure="access-patterns"]');
  expect(name.compareDocumentPosition(count) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
});

test('known cost and savings show their tiles', () => {
  render();
  expect(container.querySelector('[data-tile="projected-cost"]').textContent).toBe('Projected cost$582.76/mo');
  expect(container.querySelector('[data-tile="current-cost"]').textContent).toContain(KNOWN.currentCostStat);
  expect(container.querySelector('[data-tile="savings"]').textContent).toContain(KNOWN.savingsStat);
});

test('an unknown cost baseline removes the current cost and savings tiles, with no placeholder', () => {
  render({ costBaseline: UNKNOWN });
  expect(tiles()).toEqual(['database', 'target-engines', 'projected-cost']);
  expect(container.textContent).not.toContain('source cost not provided');
  expect(container.textContent).not.toContain('not available');
});

test('a missing projected cost removes its tile instead of showing a dash', () => {
  [null, undefined, NaN].forEach((missing) => {
    render({ projectedCost: missing, costBaseline: UNKNOWN });
    expect(tiles()).toEqual(['database', 'target-engines']);
    expect(container.textContent).not.toContain('Projected cost');
  });
});

test('a projected cost of 0 is a figure, and its tile shows it', () => {
  render({ projectedCost: 0, costBaseline: UNKNOWN });
  expect(tiles()).toEqual(['database', 'target-engines', 'projected-cost']);
  expect(container.querySelector('[data-tile="projected-cost"]').textContent).toBe('Projected cost$0.00/mo');
});

test('the count notes: queries behind the patterns and cache-layer patterns', () => {
  render({ accessPatternCount: 64, ownerQueryCount: 107, cachePatternCount: 10 });
  const db = container.querySelector('[data-tile="database"]');
  expect(db.querySelector('[data-figure="access-patterns"]').textContent).toBe('64');
  expect(db.textContent).toContain(t('analysis-results.executive-summary.access-patterns-of-queries', { count: 107 }));
  expect(db.textContent).toContain(t('analysis-results.executive-summary.cache-patterns-note', { count: 10 }));
});

test('without schema design the count is the queries, and with neither it is a dash', () => {
  render({ accessPatternCount: 0, ownerQueryCount: 107 });
  expect(container.querySelector('[data-figure="access-patterns"]').textContent).toBe('107');
  render({ accessPatternCount: 0, ownerQueryCount: 0, databaseName: null });
  expect(container.querySelector('[data-figure="access-patterns"]').textContent).toBe('—');
  expect(container.querySelector('[data-figure="database-name"]').textContent).toBe('—');
});

const RETIRED = [
  { engine: 'documentdb', monthly_cost_usd: 367.39, moved_to: ['aurora_mysql'], reason: 'DocumentDB provides no unique capabilities over Aurora MySQL' },
  { engine: 'opensearch', monthly_cost_usd: null, moved_to: ['aurora_mysql'], reason: null },
];

test('#1076: an engine the customer runs today and the plan retires has its own tile, with its saving and reason', () => {
  render({ retiredEngines: RETIRED });
  expect(tiles()).toContain('retired-engines');
  const tile = container.querySelector('[data-tile="retired-engines"]');
  expect(tile.querySelector('[data-retired-engine="documentdb"]').textContent).toBe(
    'DocumentDB (runs today), $367.39/moDocumentDB provides no unique capabilities over Aurora MySQL.',
  );
  expect(tile.querySelector('[data-retired-engine="opensearch"]').textContent).toBe(
    'OpenSearch (runs today), no saving counted',
  );
  expect(tile.textContent).toContain('Retired by this plan');
});

test('#1076: without a retired engine there is no retired tile', () => {
  [undefined, null, []].forEach((none) => {
    render({ retiredEngines: none });
    expect(tiles()).not.toContain('retired-engines');
  });
});

test('#1076: a retired engine with no counted saving says why', () => {
  render({
    retiredEngines: [
      { engine: 'opensearch', monthly_cost_usd: null, moved_to: [], reason: 'It adds nothing over Aurora MySQL.', no_saving_reason: 'no queries justified it before the move' },
    ],
  });
  expect(container.querySelector('[data-retired-engine="opensearch"]').textContent).toBe(
    'OpenSearch (runs today), no saving counted (no queries justified it before the move)It adds nothing over Aurora MySQL.',
  );
});
