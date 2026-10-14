/**
 * #615 (ADR-032): the UI formats the wave table figures synthesis computed
 * once (report.json `facts`), and falls back to the wave's own fields only
 * for a report synthesized before them.
 */
import {
  currentTradeOffs,
  openRiskCountOf,
  realityCheckDecisions,
  tradeOffsByTab,
  tradeOffTabLabel,
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
