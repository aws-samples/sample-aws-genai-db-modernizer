/**
 * #405: the embedded extractPatterns() script must only skip the cache engine's
 * own schema design when the report actually carries a cache_overlay. A legacy
 * report (produced before the overlay existed, see cacheLayer.js's header) has
 * no cache_overlay at all, and ElastiCache there is a real owner -- its design's
 * access patterns are owned workload, same as src/report/analysis_report.py
 * (which only strips a design when cache_overlay.engine names it).
 *
 * Separate file from ExportReport.cachePatterns.test.js and
 * ExportReport.cacheLayer.test.js on purpose: the embedded script declares
 * top-level `const`s in the jsdom global scope when evaluated, so it can only
 * be loaded once per test file.
 */
import { generateHTMLReport } from '../ExportReport';

const legacyReportData = () => ({
  jobId: 'job-1',
  exportDate: '2026-10-03T12:00:00Z',
  collector: {},
  schemaDesigns: [
    { target_type: 'dynamodb', content: { access_patterns: [{ pattern_id: 'ddb-1' }, { pattern_id: 'ddb-2' }] } },
    { target_type: 'opensearch', content: { access_patterns: [{ pattern_id: 'os-1' }] } },
    {
      target_type: 'elasticache',
      content: { access_patterns: [{ pattern_id: 'ec-1' }, { pattern_id: 'ec-2' }, { pattern_id: 'ec-3' }] },
    },
  ],
  queryJourneys: { items: [] },
  results: {
    synthesis: {
      database_name: 'wordpress',
      summary: 'summary',
      // No cache_overlay at all -- legacy shape.
      reality_check: { after_distribution: { dynamodb: 68, opensearch: 3, elasticache: 3 } },
      tco_analysis: { projected_monthly_cost: 823.72, cost_breakdown: [] },
    },
  },
});

/**
 * Run the exported report's own embedded <script> in this test's jsdom window.
 * Mirrors ExportReport.cachePatterns.test.js's loadInteractiveReport; kept
 * local and used exactly once (the script declares top-level consts).
 */
const loadInteractiveReport = (markup) => {
  const parsed = new DOMParser().parseFromString(markup, 'text/html');
  const inline = [...parsed.querySelectorAll('script')].filter((el) => !el.src);
  expect(inline).toHaveLength(1);
  const code = inline[0].textContent;
  parsed.querySelectorAll('script').forEach((el) => el.remove());

  window.Chart = class { destroy() {} };
  jest.spyOn(console, 'log').mockImplementation(() => {});
  document.head.innerHTML = parsed.head.innerHTML;
  document.body.innerHTML = parsed.body.innerHTML;
  const script = document.createElement('script');
  script.textContent = code;
  document.body.appendChild(script);
  document.dispatchEvent(new Event('DOMContentLoaded'));
  return window;
};

describe('embedded extractPatterns script: legacy report, no cache_overlay (#405)', () => {
  let doc;

  beforeAll(() => {
    doc = loadInteractiveReport(generateHTMLReport(legacyReportData())).document;
  });

  it('keeps the cache engine design in the pattern count driving the pie/filters/rows', () => {
    // 2 dynamodb + 1 opensearch + 3 elasticache -- nothing is stripped without an overlay.
    expect(doc.getElementById('pattern-count').textContent).toBe('6');
  });

  it('renders an ElastiCache row in the Access Pattern Explorer table, same as any other owner', () => {
    const container = doc.getElementById('access-patterns-container');
    expect(container.querySelectorAll('[data-engine="elasticache"]')).toHaveLength(3);
  });
});
