import i18next from 'i18next';
import {
  analysisConfidence,
  confidenceAlertText,
  confidenceText,
  engineConfidence,
  evidenceNote,
  hasRoutedConfidence,
  isSignalOnly,
} from '../rankingConfidence';
import { formatCacheLayerLine } from '../cacheLayer';
import en from '../../locales/en.json';

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

describe('isSignalOnly / evidenceNote / confidenceText (#312 review)', () => {
  it('labels a signal-only routed fit, long and short form', () => {
    const item = { target: 'opensearch', routed_confidence: 60, routed_confidence_evidence: 'signal_only' };
    expect(isSignalOnly(item)).toBe(true);
    expect(evidenceNote(item)).toBe('signal only — no table-level evidence');
    expect(evidenceNote(item, true)).toBe('signal only');
    expect(confidenceText(item)).toBe('60% (signal only — no table-level evidence)');
    expect(confidenceText(item, true)).toBe('60% (signal only)');
  });

  it('does not label a table-backed routed fit', () => {
    const item = { target: 'dynamodb', routed_confidence: 91, routed_confidence_evidence: 'table' };
    expect(isSignalOnly(item)).toBe(false);
    expect(evidenceNote(item)).toBe('');
    expect(confidenceText(item)).toBe('91%');
  });

  it('labels a partial fit only once a quarter of its queries lack table evidence', () => {
    const mostlyBacked = {
      routed_confidence: 91,
      routed_confidence_evidence: 'partial',
      routed_queries: 385,
      routed_queries_without_table_evidence: 17,
    };
    expect(evidenceNote(mostlyBacked)).toBe('');
    expect(confidenceText(mostlyBacked)).toBe('91%');

    const aQuarterUnbacked = { ...mostlyBacked, routed_queries_without_table_evidence: 100 };
    expect(evidenceNote(aQuarterUnbacked)).toBe('partly signal-based');
    expect(confidenceText(aQuarterUnbacked)).toBe('91% (partly signal-based)');
  });

  it('is never labelled without a routed confidence (the legacy analysis-average path)', () => {
    const item = { confidence_score: 48, routed_confidence_evidence: 'signal_only' };
    expect(isSignalOnly(item)).toBe(false);
    expect(evidenceNote(item)).toBe('');
    expect(confidenceText(item)).toBe('48%');
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
    // #225: owner shares are of query patterns, a
    // different basis than the cache's share of calls -- label it as such.
    expect(doc.body.textContent).toContain('ranked by share of query patterns');
  });

  it('labels a signal-only engine on the ranking card (#312 review: discourse OpenSearch)', () => {
    const resultsData = {
      synthesis: {
        database_name: 'discourse',
        ranking: [
          {
            target: 'opensearch',
            confidence_score: 2,
            analysis_confidence: 2,
            routed_confidence: 60,
            routed_confidence_evidence: 'signal_only',
            workload_percent: 0.2,
          },
        ],
      },
    };
    const doc = new DOMParser().parseFromString(buildReportHtml({ resultsData, jobId: 'j', t }), 'text/html');
    const card = doc.querySelector('.ranking-card').textContent;
    expect(card).toContain('60% (signal only)');
  });
});

describe('confidenceAlertText (Target Database Details, #152, #312)', () => {
  it('states the routed fit, not the analysis average', () => {
    const item = { target: 'opensearch', confidence_score: 2, routed_confidence: 60, routed_queries: 3 };
    expect(confidenceAlertText(item)).toBe(
      'The queries routed to this database fit it at 60% on average (3 queries)',
    );
  });

  it('keeps the legacy wording for a report without routed confidence', () => {
    expect(confidenceAlertText({ target: 'dynamodb', confidence_score: 48 })).toBe(
      'This database is recommended with 48% confidence based on workload analysis',
    );
  });

  it('adds the signal-only caveat (discourse OpenSearch, #312 review)', () => {
    const item = {
      target: 'opensearch',
      confidence_score: 2,
      routed_confidence: 60,
      routed_confidence_evidence: 'signal_only',
      routed_queries: 3,
    };
    expect(confidenceAlertText(item)).toBe(
      'The queries routed to this database fit it at 60% on average '
      + '(3 queries; signal only — no table-level evidence)',
    );
  });

  it('adds the partly-signal-based caveat once a quarter of its queries lack table evidence', () => {
    const item = {
      target: 'dynamodb',
      routed_confidence: 91,
      routed_confidence_evidence: 'partial',
      routed_queries: 385,
      routed_queries_without_table_evidence: 100,
    };
    expect(confidenceAlertText(item)).toBe(
      'The queries routed to this database fit it at 91% on average '
      + '(385 queries; partly signal-based)',
    );
  });
});

describe('confidenceAlertText pluralization with real i18next (#312: _one/_other, not _plural)', () => {
  const i18n = i18next.createInstance();

  beforeAll(() => i18n.init({
    lng: 'en',
    fallbackLng: 'en',
    resources: { en: { translation: en } },
    interpolation: { escapeValue: false },
    keySeparator: false,
  }));

  const t = (key, options) => i18n.t(key, options);

  it('renders "1 query" (singular), not "1 queries"', () => {
    const item = { target: 'opensearch', routed_confidence: 60, routed_queries: 1 };
    expect(confidenceAlertText(item, t)).toBe(
      'The queries routed to this database fit it at 60% on average (1 query)',
    );
  });

  it('renders "3 queries" (plural) with the signal-only caveat', () => {
    const item = {
      target: 'opensearch',
      routed_confidence: 60,
      routed_confidence_evidence: 'signal_only',
      routed_queries: 3,
    };
    expect(confidenceAlertText(item, t)).toBe(
      'The queries routed to this database fit it at 60% on average '
      + '(3 queries; signal only — no table-level evidence)',
    );
  });
});
