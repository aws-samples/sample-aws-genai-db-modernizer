/**
 * #615 (ADR-032): the UI formats the wave table figures synthesis computed
 * once (report.json `facts`), and falls back to the wave's own fields only
 * for a report synthesized before them.
 */
import {
  accessPatternTotals,
  closeCallsOf,
  currentTradeOffs,
  designTableCountOf,
  mappedTableCountOf,
  openRiskCountAt,
  openRiskCountOf,
  resolvedRiskCountOf,
  resolvedRisksBySeverityText,
  tradeOffCountOf,
  realityCheckDecisions,
  tradeOffsByTab,
  tradeOffTabLabel,
  unservableOf,
  waveFacts,
} from '../reportFacts';
import enMessages from '../../locales/en.json';

const mergedWave = {
  wave: 2,
  engines: ['dynamodb', 'documentdb'],
  table_count: 4,
  // DocumentDB serves db.t2, which DynamoDB owns (#581): the deduped list
  // has two entries, but only db.t4 is kept in sync from outside the wave.
  table_owners: [
    { table: 'db.t2', owner: 'dynamodb', sync: 'CDC' },
    { table: 'db.t4', owner: 'aurora_mysql', sync: 'CDC' },
  ],
};

const synthesis = {
  migration_waves: [mergedWave],
  facts: {
    waves: [
      { wave: 1, served_tables: 312, views: 1, tables_only: 311 },
      { wave: 2, served_tables: 4, moving_tables: 3, kept_in_sync_tables: 1 },
    ],
  },
};

test('a wave\'s facts are the stored row, or null when the report has none', () => {
  expect(waveFacts(synthesis, mergedWave)).toEqual(
    { wave: 2, served_tables: 4, moving_tables: 3, kept_in_sync_tables: 1 },
  );
  expect(waveFacts({}, mergedWave)).toBeNull();
});

test('the open-risk count is the fact, not the shown list length', () => {
  expect(openRiskCountOf({ facts: { open_risk_count: 7 } }, [{}, {}])).toBe(7);
  expect(openRiskCountOf({ facts: { open_risk_count: 0 } }, [{}])).toBe(0);
});

test('lists the reality-check decisions from facts as trade-offs (#595)', () => {
  const synthesis = {
    facts: {
      reality_check_decisions: [
        { index: 0, kind: 'kept', title: 'DocumentDB kept', detail: 'serves 3 queries', query_ids: ['q1'] },
      ],
    },
  };
  expect(realityCheckDecisions(synthesis)).toEqual([
    { description: 'DocumentDB kept', impact: 'serves 3 queries', source_tables: [], target_tables: [], query_ids: ['q1'] },
  ]);
});

test('falls back to the reality-check trade-off entries without facts', () => {
  const synthesis = {
    trade_offs: [
      { description: 'DocumentDB kept: why', impact: 'DocumentDB kept: why', engine: 'reality-check', query_ids: [] },
      { description: 'GSI overloading', impact: 'fewer tables', engine: 'dynamodb' },
    ],
  };
  const rc = realityCheckDecisions(synthesis);
  expect(rc).toHaveLength(1);
  expect(rc[0].description).toBe('DocumentDB kept: why');
  expect(rc[0].impact).toBe('');
});

test('drops the reality-check lines a later assignment edit reversed (#546)', () => {
  const synthesis = {
    trade_offs: [{ description: 'a' }, { description: 'b' }, { description: 'c' }],
    facts: { reality_check_trade_offs: [{ index: 1, reversed: true }, { index: 2, reversed: false }] },
  };
  expect(currentTradeOffs(synthesis).map(t => t.description)).toEqual(['a', 'c']);
});

test('restates a reality-check line an edit undid in part (#861)', () => {
  const synthesis = {
    trade_offs: [{ description: 'a', impact: 'a' }, { description: 'old', impact: 'old' }],
    facts: { reality_check_trade_offs: [{ index: 1, reversed: false, description_now: 'now' }] },
  };
  expect(currentTradeOffs(synthesis).map(t => [t.description, t.impact])).toEqual([['a', 'a'], ['now', 'now']]);
});

// ADR-032 Decision 7: the results page's "Reality check" tab under
// "Trade-offs and design decisions".
describe('results page Reality check tab (ADR-032 Decision 7)', () => {
  const t = (key) => enMessages[key];
  const labels = { dynamodb: 'DynamoDB' };
  const withDecisions = {
    facts: {
      reality_check_decisions: [
        { title: 'Consolidated 2 queries from DocumentDB to Aurora MySQL', detail: 'No unique capabilities.', query_ids: ['q1', 'q2'] },
        { title: 'Cache the hot reads', detail: '' },
      ],
    },
  };
  const designs = { dynamodb: [{ description: 'Single table design' }] };

  test('adds a Reality check tab, labelled with its decision count, after the engines', () => {
    const tabs = tradeOffsByTab(designs, withDecisions);
    expect(Object.keys(tabs)).toEqual(['dynamodb', 'reality-check']);
    expect(tabs['reality-check'].map(d => d.description)).toEqual([
      'Consolidated 2 queries from DocumentDB to Aurora MySQL',
      'Cache the hot reads',
    ]);
    expect(tabs['reality-check'][0].query_ids).toEqual(['q1', 'q2']);
    expect(tradeOffTabLabel('reality-check', tabs['reality-check'].length, t, labels)).toBe('Reality check (2)');
    expect(tradeOffTabLabel('dynamodb', 1, t, labels)).toBe('DynamoDB (1)');
  });

  test('adds no tab when the reality check decided nothing', () => {
    expect(Object.keys(tradeOffsByTab(designs, { facts: { reality_check_decisions: [] } }))).toEqual(['dynamodb']);
    expect(Object.keys(tradeOffsByTab(designs, undefined))).toEqual(['dynamodb']);
  });
});

describe('the counts the deliverables print are facts (#748)', () => {
  const facts = {
    open_risks_by_severity: { CRITICAL: 1, MEDIUM: 4 },
    resolved_risk_count: 6,
    trade_off_count: 12,
    table_basis: { mapped_tables: 22 },
  };

  test('read the stored fact, not the length of the list on screen', () => {
    expect(openRiskCountAt({ facts }, 'CRITICAL', [])).toBe(1);
    expect(openRiskCountAt({ facts }, 'HIGH', [{}, {}])).toBe(0);
    expect(resolvedRiskCountOf({ facts }, [{}])).toBe(6);
    expect(tradeOffCountOf({ facts }, [{}])).toBe(12);
    expect(mappedTableCountOf({ facts }, [{}])).toBe(22);
  });

  test('a hand-built report with no facts block shows the list it has', () => {
    expect(openRiskCountAt({}, 'HIGH', [{}, {}])).toBe(2);
    expect(openRiskCountAt({ facts: {} }, 'HIGH', [{}])).toBe(1);
    expect(resolvedRiskCountOf({}, [{}, {}, {}])).toBe(3);
    expect(tradeOffCountOf(undefined, [{}])).toBe(1);
    expect(mappedTableCountOf({ facts: {} }, [{}, {}])).toBe(2);
  });
});

// #1020: the resolved risks by severity are the stored fact, worded per severity.
describe('resolved risks by severity (#1020)', () => {
  const t = (key, opts) => enMessages[key].replace('{{count}}', opts.count).replace('{{severity}}', opts.severity);

  test('every stored severity, in its order, with its count', () => {
    const facts = { resolved_risks_by_severity: { HIGH: 2, MEDIUM: 4 } };
    expect(resolvedRisksBySeverityText({ facts }, t)).toBe('2 high, 4 medium');
  });

  test('a report with no such map gets no text, not a recount of the list', () => {
    expect(resolvedRisksBySeverityText({ facts: { resolved_risk_count: 6 } }, t)).toBe('');
    expect(resolvedRisksBySeverityText({}, t)).toBe('');
    expect(resolvedRisksBySeverityText({ facts: { resolved_risks_by_severity: {} } }, t)).toBe('');
  });
});

// #792: the access-pattern totals the results page shows are the stored facts.
describe('access-pattern totals (#792)', () => {
  const facts = {
    access_pattern_totals: {
      owned_pattern_count: 54,
      cache_layer_pattern_count: 10,
      listed_pattern_count: 64,
    },
  };

  test('read the stored totals, not the rows on screen', () => {
    expect(accessPatternTotals({ facts }, 3, 1)).toEqual({ owned: 54, cacheLayer: 10, listed: 64 });
  });

  test('a stored zero is a zero, not a fall back to the rows', () => {
    const zero = { access_pattern_totals: { owned_pattern_count: 0, cache_layer_pattern_count: 0, listed_pattern_count: 0 } };
    expect(accessPatternTotals({ facts: zero }, 3, 1)).toEqual({ owned: 0, cacheLayer: 0, listed: 0 });
  });

  test('a report with no such fact shows the rows it has', () => {
    const rows = { owned: 3, cacheLayer: 1, listed: 4 };
    expect(accessPatternTotals({ facts: {} }, 3, 1)).toEqual(rows);
    expect(accessPatternTotals({ facts: { access_pattern_totals: null } }, 3, 1)).toEqual(rows);
    expect(accessPatternTotals(undefined, 3, 1)).toEqual(rows);
  });
});

// #730: the close calls the callout formats are the stored facts, and a report
// without any (or written before them) has no callout at all.
test('closeCallsOf returns the stored close calls, or null without any', () => {
  const cc = { margin_points: 5, close_call_query_count: 2, engine_pairs: [{ engine: 'dynamodb', alternative: 'aurora_mysql', pair_query_count: 2 }] };
  expect(closeCallsOf({ facts: { close_calls: cc } })).toBe(cc);
  expect(closeCallsOf({ facts: { close_calls: { ...cc, engine_pairs: [] } } })).toBeNull();
  expect(closeCallsOf({ facts: { close_calls: null } })).toBeNull();
  expect(closeCallsOf({ facts: {} })).toBeNull();
  expect(closeCallsOf(null)).toBeNull();
});

// #729: the queries on a fallback engine are the stored facts; none (or a
// report written before them) shows no callout.
test('unservableOf returns the stored queries, or null without any', () => {
  const u = { unservable_query_count: 1, unservable_queries: [{ query_id: 'q1', engine: 'aurora_mysql' }] };
  expect(unservableOf({ facts: { unservable: u } })).toBe(u);
  expect(unservableOf({ facts: { unservable: { ...u, unservable_queries: [] } } })).toBeNull();
  expect(unservableOf({ facts: {} })).toBeNull();
  expect(unservableOf(null)).toBeNull();
});

describe('queryPatternCountsOf (#1146)', () => {
  const { queryPatternCountsOf } = require('../reportFacts');
  test('reads facts.query_pattern_counts', () => {
    expect(
      queryPatternCountsOf({ facts: { query_pattern_counts: { query_count: 9, pattern_count: 4 } } }),
    ).toEqual({ queries: 9, patterns: 4 });
  });
  test('is null for a report written before it', () => {
    expect(queryPatternCountsOf({ facts: {} })).toBeNull();
    expect(queryPatternCountsOf({})).toBeNull();
  });
});

describe('designTableCountOf (#1248)', () => {
  const withFacts = {
    facts: {
      engines: [
        { engine: 'aurora_mysql', target_objects: 20 },
        { engine: 'dynamodb', target_objects: 15 },
      ],
    },
  };

  it('reads the engine design table count from the facts block', () => {
    expect(designTableCountOf(withFacts, 'aurora_mysql', [{}, {}])).toBe(20);
    expect(designTableCountOf(withFacts, 'dynamodb', [])).toBe(15);
  });

  it('falls back to the shown tables for a report with no facts row', () => {
    expect(designTableCountOf({}, 'aurora_mysql', [{}, {}, {}])).toBe(3);
    expect(designTableCountOf(withFacts, 'aurora_postgresql', [{}])).toBe(1);
    expect(designTableCountOf({ facts: { engines: [{ engine: 'aurora_mysql', target_objects: null }] } }, 'aurora_mysql', [{}])).toBe(1);
  });

  it('labels the count as the tables in that design, not the source tables', () => {
    const label = enMessages['analysis-results.aurora-design.tab-label_other'];
    expect(label).toBe('{{name}} · {{count}} tables in its design');
    expect(enMessages['analysis-results.aurora-design.tab-label']).toBe('{{name}} · {{count}} table in its design');
  });
});
