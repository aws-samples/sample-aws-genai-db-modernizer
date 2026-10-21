/**
 * #1312: the pattern analysis page shows the engine overview figures the server
 * computed (the analysis `facts`) and computes none of them itself. The data below is
 * built so that every figure the page used to derive in the browser (the mean fit, the
 * counts, the signal total) comes out different from the stored fact: the page must
 * print the fact.
 *
 * Cloudscape ships ESM this repo's Jest config cannot transform, so its components are
 * mocked as plain DOM elements (the precedent is AssignmentGateSummaryFallback.test.js);
 * the page itself is real.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';

jest.mock('react-i18next', () => ({
  useTranslation: () => ({ t: (key, opts) => (opts ? `${key} ${JSON.stringify(opts)}` : key) }),
}));
jest.mock('react-router-dom', () => ({
  useParams: () => ({ jobId: 'job-1' }),
  useNavigate: () => jest.fn(),
}));

const mockExecute = jest.fn();
jest.mock('../../classes/ApiManager', () => ({
  __esModule: true,
  default: function ApiManager() {
    this.execute = mockExecute;
  },
}));
jest.mock('../../components/AppHeader', () => ({ __esModule: true, default: () => null }));
jest.mock('../../components/EngineBadge', () => ({ __esModule: true, default: () => null }));

function mockPlain() {
  return {
    __esModule: true,
    default: ({ children, header, description }) => (
      <div>
        {header}
        {description}
        {children}
      </div>
    ),
  };
}
jest.mock('@cloudscape-design/components/breadcrumb-group', () => mockPlain());
jest.mock('@cloudscape-design/components/space-between', () => mockPlain());
jest.mock('@cloudscape-design/components/container', () => mockPlain());
jest.mock('@cloudscape-design/components/box', () => mockPlain());
jest.mock('@cloudscape-design/components/column-layout', () => mockPlain());
jest.mock('@cloudscape-design/components/side-navigation', () => mockPlain());
jest.mock('@cloudscape-design/components/flashbar', () => mockPlain());
jest.mock('@cloudscape-design/components/badge', () => mockPlain());
jest.mock('@cloudscape-design/components/button', () => mockPlain());
jest.mock('@cloudscape-design/components/popover', () => mockPlain());
jest.mock('@cloudscape-design/components/segmented-control', () => mockPlain());
jest.mock('@cloudscape-design/components/table', () => mockPlain());
jest.mock('@cloudscape-design/components/header', () => ({
  __esModule: true,
  default: ({ children, description }) => (
    <h2>
      {children}
      <span className="header-description">{description}</span>
    </h2>
  ),
}));
jest.mock('@cloudscape-design/components/pie-chart', () => ({
  __esModule: true,
  default: ({ innerMetricValue }) => <div className="pie-total">{innerMetricValue}</div>,
}));
jest.mock('@cloudscape-design/components/tabs', () => ({
  __esModule: true,
  default: ({ tabs }) => (
    <div className="tabs">
      {tabs.map((tab) => (
        <div key={tab.id} className="tab-label">{tab.label}</div>
      ))}
    </div>
  ),
}));
jest.mock('@cloudscape-design/components/app-layout', () => ({
  __esModule: true,
  default: ({ content }) => <div>{content}</div>,
}));

import PatternAnalysisPage from '../PatternAnalysis';

global.IS_REACT_ACT_ENVIRONMENT = true;

const TRIAGE = {
  success: true,
  selected_agents: [{ agent_type: 'dynamodb' }],
  signals: [
    { signal: 'point_reads', query_count: 3, targets: ['dynamodb'] },
    { signal: 'other_engine', query_count: 50, targets: ['elasticache'] },
  ],
};

// Raw fields that would give a mean of 50, 2 tables, 1 pattern, 1 anti-pattern, 0 queries,
// next to facts that say 81, 7, 4, 2, 9, a signal total of 11 and 1 signal.
const ANALYSIS = {
  success: true,
  table_recommendations: [{ confidence_score: 10 }, { confidence_score: 90 }],
  workload_analysis: { patterns_detected: [{ pattern_id: 'p' }], anti_patterns_detected: [{ anti_pattern_id: 'a', query_ids: ['q'] }] },
  cost_estimate: { monthly_cost_usd: 1 },
  facts: {
    avg_confidence_score: 81,
    tables_analyzed: 7,
    monthly_cost_usd: 123.456,
    patterns_detected: 4,
    anti_patterns_detected: 2,
    anti_pattern_query_counts: [5, 6],
    queries_covered: 9,
    signal_count: 1,
    signal_query_total: 11,
  },
};

let container;
let root;

beforeEach(() => {
  window.history.pushState({}, '', '/analysis/patterns/job-1?target=dynamodb');
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  mockExecute.mockReset();
});

async function renderPage(analysis) {
  mockExecute.mockResolvedValue({
    'get-triage': TRIAGE,
    'get-collector': { success: true, queries: { query_patterns: [] } },
    'get-analysis': analysis,
  });
  await act(async () => {
    root.render(<PatternAnalysisPage />);
  });
}

test('prints the stored facts, not a figure computed from the raw fields', async () => {
  await renderPage(ANALYSIS);
  const text = container.textContent;

  expect(text).toContain('81%'); // mean of 10 and 90 would be 50
  expect(text).toContain('$123.46');
  expect(container.querySelector('.pie-total').textContent).toBe('11'); // 3 + 50 are the raw signals
  const descriptions = [...container.querySelectorAll('.header-description')].map((d) => d.textContent);
  expect(descriptions.find((d) => d.startsWith('pattern-analysis.detected-patterns.description'))).toContain('"count":1');
  expect(container.querySelector('.tab-label:last-child').textContent).toContain('"count":2');
  ['7', '9', '4'].forEach((n) => expect(text).toContain(n));
});

test('an analysis without facts shows zeros, not a figure of its own', async () => {
  const { facts, ...old } = ANALYSIS; // eslint-disable-line no-unused-vars
  await renderPage(old);

  expect(container.textContent).toContain('0%');
  expect(container.textContent).not.toContain('50%');
  expect(container.querySelector('.pie-total').textContent).toBe('0');
});

// #1325: the cache layer's "Avg cache fit" is the fit of the reads routed to it, served as a fact.
// The raw table scores below would average 50 and the analysis average fact is 81: the page must
// print the routed figure, or "not applicable" when no read is routed to the cache layer.
const CACHE_FACTS = {
  ...ANALYSIS.facts,
  routed_cache_fit: 96,
  routed_cache_queries: 4,
  routed_cache_fit_applicable: true,
};

test('the cache layer prints the routed cache fit, not the analysis average', async () => {
  window.history.pushState({}, '', '/analysis/patterns/job-1?target=elasticache');
  await renderPage({ ...ANALYSIS, facts: CACHE_FACTS });
  const text = container.textContent;

  expect(text).toContain('engine-analysis.summary.avg-cache-fit');
  expect(text).toContain('96%');
  expect(text).not.toContain('81%');
  expect(text).not.toContain('50%');
});

test('a cache layer with no routed reads shows the figure as not applicable', async () => {
  window.history.pushState({}, '', '/analysis/patterns/job-1?target=elasticache');
  await renderPage({
    ...ANALYSIS,
    facts: { ...ANALYSIS.facts, routed_cache_fit: null, routed_cache_queries: 0, routed_cache_fit_applicable: false },
  });
  const text = container.textContent;

  expect(text).toContain('engine-analysis.summary.cache-fit-not-applicable');
  expect(text).not.toContain('81%');
  expect(text).not.toContain('50%');
  expect(text).not.toContain('0%');
});

test('an old response without the routed fact never shows the analysis average for the cache layer', async () => {
  window.history.pushState({}, '', '/analysis/patterns/job-1?target=elasticache');
  await renderPage(ANALYSIS);

  expect(container.textContent).not.toContain('81%');
  expect(container.textContent).toContain('engine-analysis.summary.cache-fit-not-applicable');
});
