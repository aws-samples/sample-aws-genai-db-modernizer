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
import {
  generateHTMLReport, KEEP_COST_CARD_EXPR, CACHE_LAYER_SUFFIX, withoutCumulativeCacheNotes,
} from '../ExportReport';
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

  it('shows the safety net\'s note under the Cache Layer stat (#424)', () => {
    // #424: the gate's cache_overlay and this report's can disagree -- the
    // post-schema-design safety net drops cached reads the design doesn't
    // cover. Without the note, the exported HTML silently showed only the
    // final (lower) number.
    const data = cacheReportData();
    data.results.synthesis.cache_overlay.safety_net_notes = [
      '20 hot reads (83.4% of calls) were assigned at the assignment gate. '
      + 'The ElastiCache schema design covers 14 of them; the other 6 are no '
      + 'longer cached and stay served by their owner engine. 14 cached reads '
      + '(71.5% of calls) remain.',
    ];
    const doc = new DOMParser().parseFromString(generateHTMLReport(data), 'text/html');
    const notes = [...doc.querySelectorAll('.stat-note')].map((n) => n.textContent);
    expect(notes).toContain(data.results.synthesis.cache_overlay.safety_net_notes[0]);
  });

  it('never shows the full notes field (#459: internal, engineering-report only)', () => {
    // The customer-edit note on this field can carry a full query hash; the
    // legacy-migration note is internal too. Neither belongs in a
    // customer-facing export.
    const data = cacheReportData();
    data.results.synthesis.cache_overlay.notes = [
      'query q_8f21c carried over from the pre-overlay assignment, full hash attached',
    ];
    const doc = new DOMParser().parseFromString(generateHTMLReport(data), 'text/html');
    const notes = [...doc.querySelectorAll('.stat-note')].map((n) => n.textContent);
    expect(notes).toEqual([]);
  });

  it('never embeds the full notes field in the DATA blob either (#459 round 3)', () => {
    // Not rendered (previous test), but it must not be in the exported file's
    // embedded JSON at all -- a customer-edit note can carry a full query hash.
    const data = cacheReportData();
    data.results.synthesis.cache_overlay.notes = [
      'query q_8f21c carried over from the pre-overlay assignment, full hash attached',
    ];
    const markup = generateHTMLReport(data);
    expect(markup).not.toContain('q_8f21c');
    expect(markup).not.toContain('carried over from the pre-overlay assignment');
  });

  it('withoutCumulativeCacheNotes strips notes but keeps everything else, including safety_net_notes', () => {
    const results = {
      synthesis: {
        database_name: 'wordpress',
        cache_overlay: {
          engine: 'elasticache',
          query_count: 10,
          notes: ['customer-edit note with a full query hash'],
          safety_net_notes: ['10 hot reads (61.3% of calls) remain.'],
        },
      },
    };
    const sanitized = withoutCumulativeCacheNotes(results);
    expect(sanitized.synthesis.cache_overlay.notes).toBeUndefined();
    expect(sanitized.synthesis.cache_overlay.safety_net_notes).toEqual(results.synthesis.cache_overlay.safety_net_notes);
    expect(sanitized.synthesis.cache_overlay.engine).toBe('elasticache');
    expect(sanitized.synthesis.database_name).toBe('wordpress');
  });

  it('withoutCumulativeCacheNotes is a no-op without a notes field', () => {
    const results = { synthesis: { cache_overlay: { engine: 'elasticache' } } };
    expect(withoutCumulativeCacheNotes(results)).toBe(results);
    expect(withoutCumulativeCacheNotes(null)).toBe(null);
    expect(withoutCumulativeCacheNotes({})).toEqual({});
  });

  it('escapes a hostile safety-net note instead of rendering it as markup (#459)', () => {
    // The note is server-generated, deterministic text -- but escaping must
    // not assume that.
    const data = cacheReportData();
    data.results.synthesis.cache_overlay.safety_net_notes = [
      '<script>alert(1)</script><b>bold</b> & escaped',
    ];
    const markup = generateHTMLReport(data);
    expect(markup).not.toContain('<script>alert(1)</script>');
    const doc = new DOMParser().parseFromString(markup, 'text/html');
    // Parsed back, the payload must be inert text, not a live element.
    expect(doc.querySelectorAll('.stat-note script').length).toBe(0);
    const note = [...doc.querySelectorAll('.stat-note')].map((n) => n.textContent)
      .find((t) => t.includes('alert(1)'));
    expect(note).toBe('<script>alert(1)</script><b>bold</b> & escaped');
  });

  it('shows the note after a full drop, with no cache_overlay.engine at all (#459 round 2)', () => {
    // overlay_summary returns None with nothing cached, so cache_overlay
    // carries no `engine` key -- the Cache Layer stat card is gated on the
    // overlay object itself (truthy), not on `.engine`.
    const data = cacheReportData();
    data.results.synthesis.cache_overlay = {
      dropped_query_ids: ['q9'],
      safety_net_notes: [
        '1 hot read (10.0% of calls) was assigned at the assignment gate. '
        + 'The ElastiCache schema design covers none of them; it is no '
        + 'longer cached and stays served by its owner engine. None remain.',
      ],
    };
    const doc = new DOMParser().parseFromString(generateHTMLReport(data), 'text/html');
    const notes = [...doc.querySelectorAll('.stat-note')].map((n) => n.textContent);
    expect(notes.some((t) => t.includes('None remain.'))).toBe(true);
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
