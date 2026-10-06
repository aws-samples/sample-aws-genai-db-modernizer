/**
 * #358: generateHTMLReport's "Target Engines" badges, "Projected Cost" stat and
 * the embedded script's own cost cards (buildCostBreakdown) left the
 * cache_overlay engine out entirely -- the same bug the live Results page had
 * (src/ui/src/pages/AnalysisResults-02.js, fixed via utils/cacheLayer.js's
 * targetEngineEntries/resolveCostBreakdown). This file is shaped like the real
 * wordpress job e6a0127b's report.json.
 *
 * Separate file from exportEscaping.test.js on purpose: the embedded script
 * declares top-level `const`s (DATA, ENGINE_LABELS, ...) in the jsdom global
 * scope when evaluated, so it can only be loaded once per test file (see
 * loadInteractiveReport below) -- exportEscaping.test.js already uses its one
 * load for its own hostile-data fixture.
 */
import { generateHTMLReport, KEEP_COST_CARD_EXPR, CACHE_LAYER_SUFFIX } from '../ExportReport';
import { isKeptCostEngine } from '../cacheLayer';

const cacheReportData = () => ({
  jobId: 'job-1',
  exportDate: '2026-10-03T12:00:00Z',
  collector: {},
  schemaDesigns: [],
  queryJourneys: { items: [] },
  results: {
    synthesis: {
      database_name: 'wordpress',
      summary: 'summary',
      reality_check: { after_distribution: { dynamodb: 82, aurora_mysql: 21, opensearch: 4 } },
      cache_overlay: { engine: 'elasticache', query_count: 14, calls_per_second: 158.85, call_share_percent: 71.5 },
      tco_analysis: {
        projected_monthly_cost: 823.72,
        cost_breakdown: [
          { database: 'dynamodb', monthly_cost_usd: 98.41, pricing_mode: 'on-demand' },
          { database: 'elasticache', monthly_cost_usd: 165.55, pricing_mode: 'on-demand' },
          { database: 'opensearch', monthly_cost_usd: 240.96, pricing_mode: 'on-demand' },
          { database: 'aurora_mysql', monthly_cost_usd: 318.8, pricing_mode: 'on-demand' },
          // Neither an owner nor the cache layer -- an eliminated engine stays excluded.
          { database: 'documentdb', monthly_cost_usd: 50, pricing_mode: 'on-demand' },
        ],
      },
    },
  },
});

const targetEngineBadges = (doc) => [...doc.querySelectorAll('[data-engine]')]
  .filter(b => ['dynamodb', 'aurora_mysql', 'opensearch', 'elasticache', 'documentdb'].includes(b.getAttribute('data-engine')))
  .map(b => ({ engine: b.getAttribute('data-engine'), text: b.textContent }));

describe('generateHTMLReport shell (#358, no script execution)', () => {
  it('lists the cache layer in Target Engines, labeled distinctly, with the shared display name', () => {
    const markup = generateHTMLReport(cacheReportData());
    const doc = new DOMParser().parseFromString(markup, 'text/html');
    expect(targetEngineBadges(doc)).toEqual([
      { engine: 'dynamodb', text: 'DynamoDB' },
      { engine: 'aurora_mysql', text: 'Aurora MySQL' },
      { engine: 'opensearch', text: 'OpenSearch' },
      { engine: 'elasticache', text: 'ElastiCache (cache layer)' },
    ]);
  });

  it('reports the headline total as the report\'s own projected_monthly_cost, not a recomputed sum', () => {
    const markup = generateHTMLReport(cacheReportData());
    const doc = new DOMParser().parseFromString(markup, 'text/html');
    const stats = [...doc.querySelectorAll('.stat-card')].map(c => c.querySelector('.stat-value')?.textContent).filter(Boolean);
    // Matches the decision report / chat total: DynamoDB + Aurora MySQL + OpenSearch + ElastiCache.
    expect(stats).toContain('$823.72/mo');
  });

  it('the total is still the report\'s own figure when the cache engine has no cost_breakdown entry at all', () => {
    const data = cacheReportData();
    data.results.synthesis.tco_analysis.cost_breakdown = data.results.synthesis.tco_analysis.cost_breakdown
      .filter(cb => cb.database !== 'elasticache');
    const doc = new DOMParser().parseFromString(generateHTMLReport(data), 'text/html');
    const stats = [...doc.querySelectorAll('.stat-card')].map(c => c.querySelector('.stat-value')?.textContent).filter(Boolean);
    expect(stats).toContain('$823.72/mo');
    // Target Engines still names it (cache_overlay says it's in play) even
    // though there is, in this fixture, no cost card for it to show.
    expect(targetEngineBadges(doc)).toContainEqual({ engine: 'elasticache', text: 'ElastiCache (cache layer)' });
  });
});

/**
 * Run the exported report's own embedded <script> in this test's jsdom window,
 * the way a browser does when the file is opened. Mirrors
 * exportEscaping.test.js's loadInteractiveReport; kept local and used exactly
 * once (the script declares top-level consts, so a second load in the same
 * realm throws "already declared").
 */
const loadInteractiveReport = (markup) => {
  const parsed = new DOMParser().parseFromString(markup, 'text/html');
  const inline = [...parsed.querySelectorAll('script')].filter(el => !el.src);
  expect(inline).toHaveLength(1);
  const code = inline[0].textContent;
  parsed.querySelectorAll('script').forEach(el => el.remove());

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

describe('embedded buildCostBreakdown script (#358)', () => {
  let doc;

  beforeAll(() => {
    doc = loadInteractiveReport(generateHTMLReport(cacheReportData())).document;
  });

  it('keeps a card for every owner plus the cache layer, labeled the same way as Target Engines', () => {
    const container = doc.getElementById('cost-breakdown-container');
    const badges = [...container.querySelectorAll('[data-engine]')]
      .map(b => ({ engine: b.getAttribute('data-engine'), text: b.textContent }))
      .sort((a, b) => a.engine.localeCompare(b.engine));
    expect(badges).toEqual([
      { engine: 'aurora_mysql', text: 'Aurora MySQL' },
      { engine: 'dynamodb', text: 'DynamoDB' },
      { engine: 'elasticache', text: 'ElastiCache (cache layer)' },
      { engine: 'opensearch', text: 'OpenSearch' },
    ]);
  });

  it('drops the card for an engine that is neither an owner nor the cache layer', () => {
    const container = doc.getElementById('cost-breakdown-container');
    expect(container.querySelectorAll('[data-engine="documentdb"]')).toHaveLength(0);
  });
});

describe('KEEP_COST_CARD_EXPR (#358, the embedded script\'s mirror of isKeptCostEngine)', () => {
  it('agrees with cacheLayer.js\'s isKeptCostEngine for the same inputs', () => {
    // eslint-disable-next-line no-new-func -- evaluating the exact expression the
    // generated script embeds, to assert the two copies haven't drifted apart.
    const predicate = new Function('cb', 'afterDist', 'cacheEngine', 'return (' + KEEP_COST_CARD_EXPR + ');');
    const cases = [
      { cb: { database: 'dynamodb' }, afterDist: { dynamodb: 1 }, cacheEngine: 'elasticache' },
      { cb: { database: 'elasticache' }, afterDist: { dynamodb: 1 }, cacheEngine: 'elasticache' },
      { cb: { database: 'documentdb' }, afterDist: { dynamodb: 1 }, cacheEngine: 'elasticache' },
      { cb: { database: 'elasticache' }, afterDist: { dynamodb: 1 }, cacheEngine: null },
      { cb: { database: 'elasticache' }, afterDist: {}, cacheEngine: undefined },
    ];
    cases.forEach(({ cb, afterDist, cacheEngine }) => {
      expect(predicate(cb, afterDist, cacheEngine)).toBe(isKeptCostEngine(cb.database, afterDist, cacheEngine));
    });
  });

  // sync_report_template.py requires every `script += '...';` line in
  // generateReportScript to be its own single-quoted literal, so
  // buildCostBreakdown can't splice KEEP_COST_CARD_EXPR/CACHE_LAYER_SUFFIX in
  // at build time -- it hardcodes the same text instead. This asserts the
  // hardcoded copy embedded in the actual generated output still matches the
  // constants above, so an edit to one without the other fails here.
  it('the generated script\'s hardcoded filter and suffix still match the constants', () => {
    const markup = generateHTMLReport(cacheReportData());
    const scriptText = [...new DOMParser().parseFromString(markup, 'text/html').querySelectorAll('script')]
      .map(el => el.textContent).join('\n');
    expect(scriptText).toContain('costs.filter(cb => ' + KEEP_COST_CARD_EXPR + ')');
    expect(scriptText).toContain('(isCacheLayer ? "' + CACHE_LAYER_SUFFIX + '" : \'\')');
  });
});
