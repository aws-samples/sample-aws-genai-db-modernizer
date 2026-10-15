/**
 * #615 (ADR-032): the UI formats the wave table figures synthesis computed
 * once (report.json `facts`), and falls back to the wave's own fields only
 * for a report synthesized before them.
 */
import {
  closeCallsOf,
  currentTradeOffs,
  mappedTableCountOf,
  openRiskCountAt,
  openRiskCountOf,
  resolvedRiskCountOf,
  tradeOffCountOf,
  realityCheckDecisions,
  tradeOffsByTab,
  tradeOffTabLabel,
  unservableOf,
  waveFacts,
  waveTableStats,
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

test('reads the merged wave move count from facts, not table_count - table_owners (#581)', () => {
  expect(waveTableStats(synthesis, mergedWave)).toBe(' · 4 served, 3 move');
});

test('states wave 1 tables and views apart (#578)', () => {
  expect(waveTableStats(synthesis, { wave: 1, table_count: 312 })).toBe(
    ' · 312 source tables and views (311 tables, 1 view)',
  );
});

test('the API-filled facts give the merged wave its real move count; nothing is computed without them', () => {
  // /results fills `facts` for an older report (#615), so the export reads
  // the same 3 the decision report prints, never table_count - table_owners.
  expect(waveTableStats(synthesis, mergedWave)).toBe(' · 4 served, 3 move');
  expect(waveFacts({}, mergedWave)).toBeNull();
  expect(waveTableStats({}, mergedWave)).toBe(' · 4 source tables');
  expect(waveTableStats({}, { wave: 3, table_count: 0 })).toBe('');
  expect(waveTableStats({}, { wave: 3, table_count: 5 })).toBe(' · 5 source tables');
});

test('the open-risk count is the fact, not the export\'s own list length', () => {
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
