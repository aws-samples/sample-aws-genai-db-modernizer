// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2315 (after #1028): the executive summary's five figures, one row: Monthly
 * cost, Overall risk, Waves, Queries, Target engines, one headline figure each with its
 * unit. The query patterns and the access-pattern counts are on the Queries tab, and the
 * long notes sit behind the Target engines' Info link. Cloudscape components are plain
 * DOM stand-ins (see QueryRoutingTable.test.js); the component and the English strings
 * are real.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { createInstance } from 'i18next';
import en from '../../locales/en.json';
import ExecutiveSummaryStats from '../ExecutiveSummaryStats';
import { costBaselineStats } from '../../utils/tcoAnalysis';
import { executiveSummaryOf } from '../../utils/reportFacts';

jest.mock('@cloudscape-design/components/box', () => ({
  __esModule: true,
  default: ({ children }) => <div>{children}</div>,
}));
jest.mock('@cloudscape-design/components/space-between', () => ({
  __esModule: true,
  default: ({ children }) => <div>{children}</div>,
}));
jest.mock('@cloudscape-design/components/column-layout', () => ({
  __esModule: true,
  default: ({ children, columns, minColumnWidth }) => (
    <div className="mock-columns" data-columns={columns} data-min-width={minColumnWidth}>{children}</div>
  ),
}));
jest.mock('@cloudscape-design/components/key-value-pairs', () => ({
  __esModule: true,
  default: ({ items, columns }) => (
    <dl className="mock-kvp" data-columns={columns}>
      {items.map(item => (
        <div key={item.label} className="mock-kvp-item">
          <dt>{item.label}{item.info}</dt>
          <dd>{item.value}</dd>
        </div>
      ))}
    </dl>
  ),
}));
jest.mock('@cloudscape-design/components/status-indicator', () => ({
  __esModule: true,
  default: ({ type, children }) => <span className="mock-status" data-type={type}>{children}</span>,
}));
jest.mock('@cloudscape-design/components/popover', () => ({
  __esModule: true,
  default: ({ header, content, children }) => (
    <span className="mock-popover">{children}<span className="mock-popover-body"><b>{header}</b>{content}</span></span>
  ),
}));
jest.mock('@cloudscape-design/components/link', () => ({
  __esModule: true,
  default: ({ children, variant }) => <a className="mock-link" data-variant={variant}>{children}</a>,
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

// The wordpress sample's figures, as report.json's facts state them.
const FACTS = {
  open_risk_count: 9,
  critical_high_risk_count: 0,
  migration_wave_count: 3,
  query_total_count: 107,
  source_table_count: 50,
  executive_brief: {
    overall_risk_level: 'MEDIUM',
    decision_wave: 1,
    decision_headline: 'Approve wave 1: move wordpress to Aurora MySQL',
    waves: [
      { wave: 1, engines: ['aurora_mysql'], kind: 'homogeneous' },
      { wave: 2, engines: ['elasticache'], kind: 'cache' },
      { wave: 3, engines: ['dynamodb'], kind: 'migration' },
    ],
  },
};

function render(props = {}, facts = FACTS) {
  act(() => {
    root.render(
      <ExecutiveSummaryStats
        t={t}
        summary={executiveSummaryOf({ facts })}
        targetEngines={<span className="engines">Aurora MySQL</span>}
        projectedCost={507.39}
        costBaseline={UNKNOWN}
        {...props}
      />,
    );
  });
}

const tiles = () => [...container.querySelectorAll('[data-tile]')].map(el => el.getAttribute('data-tile'));
const labels = () => [...container.querySelectorAll('.mock-kvp-item dt')].map(el => el.firstChild.textContent);
const figure = name => container.querySelector(`[data-figure="${name}"]`);

test('one row of five figures, in the order of the decision', () => {
  render();
  // KeyValuePairs lays out at most four columns (#2458 review): five equal columns of a
  // ColumnLayout, sized by a minimum width so a narrow screen wraps, hold one item each.
  const columns = container.querySelectorAll('.mock-columns');
  expect(columns.length).toBe(1);
  expect(columns[0].getAttribute('data-columns')).toBe('5');
  expect(Number(columns[0].getAttribute('data-min-width'))).toBeGreaterThan(0);
  const pairs = [...columns[0].children];
  expect(pairs.map(el => el.className)).toEqual(Array(5).fill('mock-kvp'));
  pairs.forEach(el => expect(el.querySelectorAll('.mock-kvp-item').length).toBe(1));
  pairs.forEach(el => expect(Number(el.getAttribute('data-columns'))).toBeLessThanOrEqual(4));
  expect(labels()).toEqual(['Monthly cost', 'Overall risk', 'Waves', 'Queries', 'Target engines']);
  expect(tiles()).toEqual(['projected-cost', 'overall-risk', 'waves', 'queries', 'target-engines']);
});

test('each figure is the fact, stated once with its unit', () => {
  render();
  expect(figure('projected-cost').textContent).toBe('$507.39/mo');
  expect(figure('overall-risk').textContent).toBe('Medium');
  expect(figure('overall-risk').querySelector('.mock-status').getAttribute('data-type')).toBe('warning');
  expect(figure('open-risks').textContent).toBe('9 open risks, none critical or high');
  expect(figure('wave-count').textContent).toBe('3');
  expect(figure('wave-engines').textContent).toBe('Aurora MySQL, ElastiCache, DynamoDB');
  expect(figure('query-count').textContent).toBe('107');
  expect(figure('source-tables').textContent).toBe('across 50 source tables');
});

test('the pattern breakdown is not in the summary: it is on the Queries tab', () => {
  render({ cacheLayerLine: 'Cache layer · 19 designed cached reads · 82.1% of calls' });
  ['access-patterns', 'query-patterns', 'database-name'].forEach(name => expect(figure(name)).toBeNull());
  expect(container.textContent).not.toMatch(/access pattern|distinct query pattern/);
});

test('critical or high risks are counted by the facts, and a level with none shows a dash', () => {
  render({}, { ...FACTS, open_risk_count: 4, critical_high_risk_count: 2, executive_brief: { ...FACTS.executive_brief, overall_risk_level: 'HIGH' } });
  expect(figure('overall-risk').textContent).toBe('High');
  expect(figure('overall-risk').querySelector('.mock-status').getAttribute('data-type')).toBe('error');
  expect(figure('open-risks').textContent).toBe('4 open risks, 2 critical or high');
  render({}, { ...FACTS, open_risk_count: 1, critical_high_risk_count: 1 });
  expect(figure('open-risks').textContent).toBe('1 open risk, 1 critical or high');
  render({}, { ...FACTS, open_risk_count: 0 });
  expect(figure('open-risks').textContent).toBe('0 open risks');
  render({}, {});
  expect(figure('overall-risk').textContent).toBe('—');
  expect(figure('open-risks')).toBeNull();
  expect(figure('wave-count').textContent).toBe('—');
  expect(figure('query-count').textContent).toBe('—');
  expect(figure('source-tables')).toBeNull();
});

test('a report saved before the brief takes the risk level from its risk assessment', () => {
  act(() => {
    root.render(
      <ExecutiveSummaryStats
        t={t}
        summary={executiveSummaryOf({ facts: { open_risk_count: 2 }, risk_assessment: { overall_risk_level: 'low' } })}
        projectedCost={1}
        costBaseline={UNKNOWN}
      />,
    );
  });
  expect(figure('overall-risk').textContent).toBe('Low');
});

test('a wave with two engines names both', () => {
  render({}, { ...FACTS, executive_brief: { ...FACTS.executive_brief, waves: [{ wave: 1, engines: ['aurora_mysql', 'elasticache'] }] } });
  expect(figure('wave-engines').textContent).toBe('Aurora MySQL + ElastiCache');
});

test('a large query count is grouped', () => {
  render({}, { ...FACTS, query_total_count: 1654, source_table_count: 1 });
  expect(figure('query-count').textContent).toBe('1,654');
  expect(figure('source-tables').textContent).toBe('across 1 source table');
});

test('a known cost baseline states the current cost and the saving under the monthly cost', () => {
  render({ costBaseline: KNOWN });
  expect(figure('current-cost').textContent).toBe(
    `Current monthly cost ${KNOWN.currentCostStat}, savings ${KNOWN.savingsStat}`,
  );
  expect(container.querySelector('[data-tile="projected-cost"]').contains(figure('current-cost'))).toBe(true);
});

test('an unknown cost baseline states no current cost, with no placeholder', () => {
  render({ costBaseline: UNKNOWN });
  expect(figure('current-cost')).toBeNull();
  expect(container.textContent).not.toContain('source cost not provided');
  expect(container.textContent).not.toContain('not available');
});

test('a missing projected cost shows a dash, and 0 is a figure', () => {
  [null, undefined, NaN].forEach((missing) => {
    render({ projectedCost: missing });
    expect(figure('projected-cost').textContent).toBe('—');
  });
  render({ projectedCost: 0 });
  expect(figure('projected-cost').textContent).toBe('$0.00/mo');
});

describe('the target engines', () => {
  test('show the badges and the cache line, with the long notes behind an Info link', () => {
    const role = 'ElastiCache is recommended as a cache-aside layer in front of the engine that owns each cached read.';
    render({
      cacheLayerLine: 'Cache layer · 19 designed cached reads · 82.1% of calls',
      targetEngineNotes: [role, '20 cache candidates, 19 designed; it stays on its owner engine.', null, false],
    });
    const tile = container.querySelector('[data-tile="target-engines"]');
    expect(tile.id).toBe('target-engines');
    expect(tile.querySelector('.engines').textContent).toBe('Aurora MySQL');
    expect(figure('cache-layer-line').textContent).toBe('Cache layer · 19 designed cached reads · 82.1% of calls');
    // The notes are in the popover, not in the tile.
    expect(tile.textContent).not.toContain(role);
    const info = figure('target-engines-info');
    expect(info.querySelector('.mock-link').getAttribute('data-variant')).toBe('info');
    expect(info.textContent).toBe('Info');
    const body = container.querySelector('.mock-popover-body');
    expect(body.textContent).toBe(
      `About the target engines${role}20 cache candidates, 19 designed; it stays on its owner engine.`,
    );
  });

  test('#1252: the cache change stays on the tile as a warning, not behind the Info link', () => {
    const change = 'The cache layer covers 19 of the 20 cache candidates the gate listed.';
    render({
      cacheLayerLine: 'Cache layer · 19 designed cached reads · 82.1% of calls',
      cacheChange: change,
      targetEngineNotes: ['ElastiCache is recommended as a cache-aside layer.'],
    });
    const tile = container.querySelector('[data-tile="target-engines"]');
    const warning = tile.querySelector('[data-figure="cache-change"]');
    expect(warning.textContent).toBe(change);
    expect(warning.querySelector('.mock-status').getAttribute('data-type')).toBe('warning');
    expect(container.querySelector('.mock-popover-body').textContent).not.toContain(change);
  });

  test('without a cache change there is no warning', () => {
    [undefined, null, ''].forEach((cacheChange) => {
      render({ cacheChange });
      expect(figure('cache-change')).toBeNull();
    });
  });

  test('without a note there is no Info link', () => {
    [undefined, [], [null, false, '']].forEach((targetEngineNotes) => {
      render({ targetEngineNotes });
      expect(figure('target-engines-info')).toBeNull();
    });
  });
});

const RETIRED = [
  { engine: 'documentdb', monthly_cost_usd: 367.39, moved_to: ['aurora_mysql'], reason: 'DocumentDB provides no unique capabilities over Aurora MySQL' },
  { engine: 'opensearch', monthly_cost_usd: null, moved_to: ['aurora_mysql'], reason: null },
];

test('#1076: an engine the customer runs today and the plan retires is stated under the monthly cost, with its saving and reason', () => {
  render({ retiredEngines: RETIRED });
  const tile = container.querySelector('[data-tile="projected-cost"]');
  expect(tile.querySelector('[data-retired-engine="documentdb"]').textContent).toBe(
    'DocumentDB (runs today), $367.39/mo DocumentDB provides no unique capabilities over Aurora MySQL.',
  );
  expect(tile.querySelector('[data-retired-engine="opensearch"]').textContent).toBe(
    'OpenSearch (runs today), no saving counted',
  );
});

test('#1076: without a retired engine none is stated', () => {
  [undefined, null, []].forEach((none) => {
    render({ retiredEngines: none });
    expect(container.querySelector('[data-retired-engine]')).toBeNull();
  });
});

test('#1076: a retired engine with no counted saving says why', () => {
  render({
    retiredEngines: [
      { engine: 'opensearch', monthly_cost_usd: null, moved_to: [], reason: 'It adds nothing over Aurora MySQL.', no_saving_reason: 'no queries justified it before the move' },
    ],
  });
  expect(container.querySelector('[data-retired-engine="opensearch"]').textContent).toBe(
    'OpenSearch (runs today), no saving counted (no queries justified it before the move) It adds nothing over Aurora MySQL.',
  );
});

test('#1164: a retired engine reason is worded as the PDF words it: trimmed, one period, none when empty', () => {
  const reasonOf = (reason) => {
    render({
      retiredEngines: [{ engine: 'opensearch', monthly_cost_usd: 5, moved_to: [], reason }],
    });
    return container.querySelector('[data-retired-engine="opensearch"]').textContent;
  };
  expect(reasonOf('x. ')).toMatch(/x\.$/);
  expect(reasonOf('x. ')).not.toMatch(/x\. \.$/);
  expect(reasonOf('x...')).toMatch(/[^.]x\.$/);
  expect(reasonOf('  ')).not.toMatch(/\.$/);
  expect(reasonOf('')).not.toMatch(/\.$/);
});

// #1328: the customer's engine scope, the line every deliverable states.
describe('the engine scope line', () => {
  const scopeLine = () => container.querySelector('[data-figure="engine-scope"]');

  test('names the scope engines by display name, in the facts order, as the customer\'s choice', () => {
    render({ engineScope: ['aurora_postgresql', 'dynamodb'] });
    expect(scopeLine().textContent).toBe("Engines in scope: Aurora PostgreSQL, DynamoDB (customer's choice)");
    expect(container.querySelector('[data-tile="target-engines"]').contains(scopeLine())).toBe(true);
  });

  test('shows nothing for a job with no scope or a report written before it', () => {
    [null, undefined, []].forEach((engineScope) => {
      render({ engineScope });
      expect(scopeLine()).toBeNull();
    });
  });
});

// #1329: the queries with no engine in the customer's engine scope, in the headline
// and next to the projected cost, from the facts the reports print.
describe('the no-engine-in-scope line', () => {
  test('names the placed, total and no-engine counts and the cost exclusion', () => {
    render({ projectedCost: 98.41, noEngineInScope: { count: 42, placed: 65, total: 107 } });
    expect(container.querySelector('[data-tile="queries"]').contains(figure('no-engine-in-scope'))).toBe(true);
    expect(container.querySelector('[data-tile="projected-cost"]').contains(figure('cost-excludes-no-engine'))).toBe(true);
    expect(container.querySelector('[data-figure="no-engine-in-scope"]').textContent).toBe(
      "65 of 107 queries placed on an engine; 42 have no engine in the customer's engine scope",
    );
    expect(container.querySelector('[data-figure="cost-excludes-no-engine"]').textContent).toBe(
      "Excludes the 42 queries with no engine in the customer's engine scope, which keep running on the source database.",
    );
  });

  test('shows nothing when every query has an engine', () => {
    [null, undefined, { count: 0, placed: 107, total: 107 }].forEach((noEngineInScope) => {
      render({ projectedCost: 98.41, noEngineInScope });
      expect(container.querySelector('[data-figure="no-engine-in-scope"]')).toBeNull();
      expect(container.querySelector('[data-figure="cost-excludes-no-engine"]')).toBeNull();
    });
  });
});

// #1196: the monthly cost states the price table's source, region and date.
const PRICE_LIST = { source: 'AWS Price List', region: 'us-east-1', pricing_model: 'on-demand', retrieved_on: '2026-10-19' };

test('the projected-cost tile states the list prices it was estimated from', () => {
  render({ priceList: PRICE_LIST });
  expect(container.querySelector('[data-figure="price-list"]').textContent)
    .toBe('List prices: AWS Price List, us-east-1 on-demand, retrieved 2026-10-19');
});

test('a report written before the price date existed states no date', () => {
  [undefined, null, {}].forEach((priceList) => {
    render({ priceList });
    expect(container.querySelector('[data-figure="price-list"]')).toBeNull();
    expect(figure('projected-cost').textContent).toBe('$507.39/mo');
  });
});

test('estimates priced from different tables name each engine\'s date', () => {
  render({ priceList: {
    source: 'AWS Price List', region: 'us-east-1', pricing_model: 'on-demand', retrieved_on: null,
    engine_dates: { aurora_mysql: '2026-10-19', dynamodb: '2026-07-01' }, undated_engines: ['elasticache'],
  } });
  expect(container.querySelector('[data-figure="price-list"]').textContent).toBe(
    'List prices: AWS Price List, us-east-1 on-demand, retrieved 2026-10-19 (Aurora MySQL), '
    + '2026-07-01 (DynamoDB); not recorded for ElastiCache',
  );
});

// #1474: the projected-cost tile says it is the full-modernization estimate. #2316: the
// pricing caveat is printed once, in the cost section, so the tile does not repeat it.
test('the projected-cost tile labels the figure and leaves the caveat to the cost section', () => {
  render({ costScopeLabel: 'Full-modernization estimate: all waves done.', costCaveat: 'Check your own pricing.' });
  expect(container.querySelector('[data-figure="cost-scope"]').textContent).toBe('Full-modernization estimate: all waves done.');
  expect(container.querySelector('[data-figure="cost-caveat"]')).toBeNull();
});

test('no label or caveat is shown when none is given', () => {
  render({});
  expect(container.querySelector('[data-figure="cost-scope"]')).toBeNull();
  expect(container.querySelector('[data-figure="cost-caveat"]')).toBeNull();
});
