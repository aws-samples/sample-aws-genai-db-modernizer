import { analysisConfidence, engineConfidence, hasRoutedConfidence } from '../rankingConfidence';
import { formatCacheLayerLine } from '../cacheLayer';

describe('engineConfidence (#152)', () => {
  it('shows the routed fit, not the all-tables average', () => {
    const item = { target: 'opensearch', confidence_score: 2, analysis_confidence: 2, routed_confidence: 60 };
    expect(engineConfidence(item)).toBe(60);
    expect(hasRoutedConfidence(item)).toBe(true);
    expect(analysisConfidence(item)).toBe(2);
  });

  it('falls back to confidence_score for a legacy report or no routed query', () => {
    expect(engineConfidence({ confidence_score: 48 })).toBe(48);
    expect(engineConfidence({ confidence_score: 40, routed_confidence: null })).toBe(40);
    expect(hasRoutedConfidence({ confidence_score: 40 })).toBe(false);
    expect(engineConfidence({})).toBe(0);
    expect(analysisConfidence({})).toBeNull();
  });
});

describe('formatCacheLayerLine cache fit (#152)', () => {
  it('adds the cache fit of a cache-layer ranking entry', () => {
    const entry = {
      role: 'cache_layer',
      cache_overlay_queries: 20,
      cache_call_share_percent: 83.4,
      routed_confidence: 79,
    };
    expect(formatCacheLayerLine(entry, { withLabel: false })).toBe(
      '20 cached reads · 83.4% of calls · 79% cache fit',
    );
  });

  it('leaves a line without a routed confidence unchanged', () => {
    expect(formatCacheLayerLine({ query_count: 3, call_share_percent: 25.1 }, { withLabel: false })).toBe(
      '3 cached reads · 25.1% of calls',
    );
  });
});

describe('buildReportHtml ranking (#152)', () => {
  // eslint-disable-next-line global-require
  const { buildReportHtml } = require('../ReportHtmlExport');
  const t = (key, opts = {}) => {
    let text = opts.defaultValue !== undefined ? opts.defaultValue : key;
    Object.entries(opts).forEach(([k, v]) => { text = text.split(`{{${k}}}`).join(v); });
    return text;
  };

  it('shows the routed fit with the analysis average for audit', () => {
    const resultsData = {
      synthesis: {
        database_name: 'discourse',
        ranking: [
          { target: 'aurora_postgresql', confidence_score: 67, analysis_confidence: 67, routed_confidence: 93, workload_percent: 76.5 },
          { target: 'opensearch', confidence_score: 2, analysis_confidence: 2, routed_confidence: 60, workload_percent: 0.2 },
        ],
      },
    };
    const doc = new DOMParser().parseFromString(buildReportHtml({ resultsData, jobId: 'j', t }), 'text/html');
    const cards = [...doc.querySelectorAll('.ranking-card')].map(c => c.textContent);
    expect(cards[1]).toContain('60%');
    expect(cards[1]).toContain('Fit of routed queries');
    expect(cards[1]).toContain('Analysis average 2%');
    expect(doc.body.textContent).toContain('ranked by share of the workload');
  });
});
