/**
 * #615 (ADR-032): the UI formats the wave table figures synthesis computed
 * once (report.json `facts`), and falls back to the wave's own fields only
 * for a report synthesized before them.
 */
import { openRiskCountOf, waveFacts, waveTableStats } from '../reportFacts';

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
