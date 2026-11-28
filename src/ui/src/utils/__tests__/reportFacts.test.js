// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #615 (ADR-032): the UI formats the wave table figures synthesis computed
 * once (report.json `facts`), and falls back to the wave's own fields only
 * for a report synthesized before them.
 */
import {
  cacheLayerFactsOf,
  designChangesOf,
  designLeftOutOf,
  derivedCopySyncOf,
  accessPatternTotals,
  closeCallsOf,
  currentTradeOffs,
  mappedTableCountOf,
  openRiskCountAt,
  openRiskCountOf,
  resolvedRiskCountOf,
  resolvedRisksBySeverityText,
  tradeOffCountOf,
  realityCheckDecisions,
  rateWindowStatementOf,
  rateWindowScenarioOf,
  rateWindowRiskNoteOf,
  assumptionLinesOf,
  assumptionItemsOf,
  assumptionValueText,
  costSizingBasisText,
  costTableOf,
  costCaveatPartsOf,
  engineRoleOf,
  growthScenarioLinesOf,
  priceListOf,
  rowCountStatementOf,
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

  test("an engine's tab lists the report's stated-once trade-offs when synthesis wrote them", () => {
    const gsi = 'A GSI is eventually consistent: a query on it returns a write once DynamoDB has applied it to the index.';
    const raw = { dynamodb: [{ description: `A. ${gsi}` }, { description: `B. ${gsi}` }], opensearch: [{ description: 'Own list' }] };
    const synthesis = { trade_offs: [
      { description: `A. ${gsi}`, engine: 'dynamodb' },
      { description: 'B.', engine: 'dynamodb' },
      { description: 'Kept the cache.', engine: 'reality-check' },
    ] };
    const tabs = tradeOffsByTab(raw, synthesis);
    expect(tabs.dynamodb.map(d => d.description)).toEqual([`A. ${gsi}`, 'B.']);
    expect(tabs.opensearch.map(d => d.description)).toEqual(['Own list']);
  });

  // #2324: synthesis drops a placeholder trade-off, so a design the report covers can
  // have none left; its tab is empty, never the design's own raw list.
  test("an engine the report covers with no trade-off left gets an empty tab", () => {
    const raw = { aurora_mysql: [{ description: 'A robust merge.' }], dynamodb: [{ description: 'Raw' }] };
    const synthesis = {
      schema_designs: { aurora_mysql: {}, dynamodb: {} },
      trade_offs: [{ description: 'Single table.', engine: 'dynamodb' }],
    };
    const tabs = tradeOffsByTab(raw, synthesis);
    expect(tabs.aurora_mysql).toEqual([]);
    expect(tabs.dynamodb.map(d => d.description)).toEqual(['Single table.']);
  });

  test('a design the report does not cover keeps its own list', () => {
    const raw = { opensearch: [{ description: 'Own list' }] };
    const synthesis = { schema_designs: { dynamodb: {} }, trade_offs: [] };
    expect(tradeOffsByTab(raw, synthesis).opensearch.map(d => d.description)).toEqual(['Own list']);
  });

  test('the empty tab names the engine', () => {
    expect(t('analysis-results.tradeoffs.empty').replace('{{engine}}', 'Aurora MySQL'))
      .toBe('No design trade-offs recorded for Aurora MySQL');
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

describe('design tab label (#1248)', () => {
  it('labels the count as the tables in that design, not the source tables', () => {
    const label = enMessages['analysis-results.aurora-design.tab-label_other'];
    expect(label).toBe('{{name}} · {{count}} tables in its design');
    expect(enMessages['analysis-results.aurora-design.tab-label']).toBe('{{name}} · {{count}} table in its design');
  });
});

// #1252: the cache layer's two read figures are stored facts, each labelled for what it counts.
describe('cache layer facts (#1252)', () => {
  const facts = {
    cache_layer: {
      engine: 'elasticache',
      candidate_read_count: 20,
      candidate_call_share_percent: 83.4,
      designed_read_count: 14,
      designed_call_share_percent: 76.1,
      owner_only_read_count: 6,
    },
  };

  test('read the candidates and the designed cached reads as stored', () => {
    expect(cacheLayerFactsOf({ facts })).toEqual({
      candidates: 20, candidateCallShare: 83.4, designed: 14, designedCallShare: 76.1, ownerOnly: 6,
      roleSentence: null,
    });
  });

  // #399: the cache layer as the assessment's recommendation, printed as stored.
  test('carries the stored role sentence, and none for a report written before it', () => {
    const sentence = 'ElastiCache is recommended as a cache-aside layer in front of each owner.';
    const stored = { cache_layer: { designed_read_count: 14, role_sentence: sentence } };
    expect(cacheLayerFactsOf({ facts: stored }).roleSentence).toBe(sentence);
    const older = { cache_layer: { designed_read_count: 14 } };
    expect(cacheLayerFactsOf({ facts: older }).roleSentence).toBeNull();
  });

  test('a report with no cache layer, or written before the fact, has none', () => {
    expect(cacheLayerFactsOf({ facts: { cache_layer: null } })).toBeNull();
    expect(cacheLayerFactsOf({ facts: {} })).toBeNull();
    expect(cacheLayerFactsOf(undefined)).toBeNull();
  });
});

// #1474: the full-modernization label and the one cost caveat (facts.cost_scope_label,
// facts.cost_caveat); a report written before them gets the same words.
describe('cost label and caveat', () => {
  const { costScopeLabelOf, costCaveatOf, COST_SCOPE_LABEL, COST_CAVEAT } = require('../reportFacts');

  test('print the stored words', () => {
    const synthesis = { facts: { cost_scope_label: 'Whole plan.', cost_caveat: 'Check prices.' } };
    expect(costScopeLabelOf(synthesis)).toBe('Whole plan.');
    expect(costCaveatOf(synthesis)).toBe('Check prices.');
  });

  test('an older report gets the same words', () => {
    [null, {}, { facts: {} }, { facts: { cost_scope_label: null, cost_caveat: null } }].forEach((synthesis) => {
      expect(costScopeLabelOf(synthesis)).toBe(COST_SCOPE_LABEL);
      expect(costCaveatOf(synthesis)).toBe(COST_CAVEAT);
    });
    expect(COST_SCOPE_LABEL).toBe('Full-modernization estimate: all waves done, every kept engine running.');
    expect(COST_CAVEAT).toContain('https://calculator.aws/');
  });
});

// #2107: the one sentence the wave plan says about the cutover (facts.cutover_responsibility);
// a report written before it gets the same words.
describe('cutover responsibility', () => {
  const { cutoverResponsibilityOf, CUTOVER_RESPONSIBILITY } = require('../reportFacts');

  test('prints the stored words', () => {
    expect(cutoverResponsibilityOf({ facts: { cutover_responsibility: 'Yours to run.' } })).toBe('Yours to run.');
  });

  test('an older report gets the same words', () => {
    [null, {}, { facts: {} }, { facts: { cutover_responsibility: null } }].forEach((synthesis) => {
      expect(cutoverResponsibilityOf(synthesis)).toBe(CUTOVER_RESPONSIBILITY);
    });
    expect(CUTOVER_RESPONSIBILITY).toBe('The cutover is your responsibility.');
  });
});

describe('derivedCopySyncOf (#1527)', () => {
  const sync = (riskId, extra = {}) => ({
    risk_id: riskId,
    path_line: `Sync path ${riskId}`,
    cost_line: `Sync cost ${riskId}`,
    ...extra,
  });
  const withSyncs = {
    facts: { derived_copy_sync: { syncs: [sync('RISK-002'), sync('RISK-005')], assumptions: ['One.', 'Two.'] } },
  };

  test('is the sync listed under the risk, with the basis on the first only', () => {
    expect(derivedCopySyncOf(withSyncs, 'RISK-002')).toEqual({
      lines: ['Sync path RISK-002', 'Sync cost RISK-002'],
      basis: 'One. Two.',
    });
    expect(derivedCopySyncOf(withSyncs, 'RISK-005').basis).toBe('');
  });

  test('is null for another risk, a risk with no id, and a report without the fact', () => {
    expect(derivedCopySyncOf(withSyncs, 'RISK-001')).toBeNull();
    expect(derivedCopySyncOf(withSyncs, undefined)).toBeNull();
    expect(derivedCopySyncOf({ facts: { derived_copy_sync: null } }, 'RISK-002')).toBeNull();
    expect(derivedCopySyncOf({ facts: {} }, 'RISK-002')).toBeNull();
    expect(derivedCopySyncOf({}, 'RISK-002')).toBeNull();
  });
});

describe('assumptionLinesOf (#1990)', () => {
  test('is the assumptions lines, then the growth scenario lines, as the facts state them', () => {
    const synthesis = {
      facts: {
        assumptions: { assumptions: [], lines: ['Growth: 5× (default)', 'Peak: not measured (default)'] },
        growth_scenarios: { rows: [], lines: ['Growth scenario: 1× $10.00/mo (the collected traffic)'] },
      },
    };
    expect(assumptionLinesOf(synthesis)).toEqual([
      'Growth: 5× (default)',
      'Peak: not measured (default)',
      'Growth scenario: 1× $10.00/mo (the collected traffic)',
    ]);
  });

  test('is empty for a report written before the facts', () => {
    expect(assumptionLinesOf({ facts: {} })).toEqual([]);
    expect(assumptionLinesOf({})).toEqual([]);
    expect(assumptionLinesOf(null)).toEqual([]);
  });
});

describe('rateWindowStatementOf (#1893)', () => {
  const statement =
    "execution counts are divided by each statement's own observed window (156 statements), the server uptime (82 statements), or an assumed 24 hours (0 statements); costs assume that rate runs continuously";

  test('is the facts sentence as it is', () => {
    expect(rateWindowStatementOf({ facts: { rate_windows: { observed_count: 156, statement } } })).toBe(statement);
  });

  test('is null for a collection with no statements, and for a report without the fact', () => {
    expect(rateWindowStatementOf({ facts: { rate_windows: { statement_count: 0, statement: null } } })).toBeNull();
    expect(rateWindowStatementOf({ facts: { rate_windows: null } })).toBeNull();
    expect(rateWindowStatementOf({ facts: {} })).toBeNull();
    expect(rateWindowStatementOf({})).toBeNull();
    expect(rateWindowStatementOf(null)).toBeNull();
  });

  test('the results page labels it', () => {
    expect(enMessages['analysis-results.cost-breakdown.rate-windows']).toBe('Call rates');
  });
});

describe('rowCountStatementOf (#1636)', () => {
  const statement =
    "265 of 311 source tables have no row count in the source's statistics; sizing takes each one's rows as its collected data size divided by its declared row width (each column at its declared size, each string at its full declared length up to 1 KB), not as 0";

  test('is the facts sentence as it is', () => {
    expect(rowCountStatementOf({ facts: { row_counts: { unknown_count: 265, statement } } })).toBe(statement);
  });

  test('is null when every count is known, and for a report without the fact', () => {
    expect(rowCountStatementOf({ facts: { row_counts: { unknown_count: 0, statement: null } } })).toBeNull();
    expect(rowCountStatementOf({ facts: { row_counts: null } })).toBeNull();
    expect(rowCountStatementOf({ facts: {} })).toBeNull();
    expect(rowCountStatementOf({})).toBeNull();
    expect(rowCountStatementOf(null)).toBeNull();
  });
});

describe('rateWindowScenarioOf (#1991)', () => {
  it('is the label the facts put on the costs and risks of a window that is not representative', () => {
    const label = 'Scenario at the measured rate: the rate window is not representative';
    expect(rateWindowScenarioOf({ facts: { rate_windows: { representative: false, scenario_label: label } } })).toBe(label);
  });

  it('is null for a representative window and for a report written before the check', () => {
    expect(rateWindowScenarioOf({ facts: { rate_windows: { representative: true, scenario_label: null } } })).toBeNull();
    expect(rateWindowScenarioOf({ facts: { rate_windows: { statement: 'x' } } })).toBeNull();
    expect(rateWindowScenarioOf({ facts: {} })).toBeNull();
    expect(rateWindowScenarioOf(null)).toBeNull();
  });
});

describe('rateWindowRiskNoteOf (#1991)', () => {
  it('is the short note the risks carry, and null without one', () => {
    const note = 'The rate-driven risks are a scenario at the measured rate too';
    expect(rateWindowRiskNoteOf({ facts: { rate_windows: { scenario_risk_note: note } } })).toBe(note);
    expect(rateWindowRiskNoteOf({ facts: { rate_windows: { scenario_risk_note: null } } })).toBeNull();
    expect(rateWindowRiskNoteOf({ facts: {} })).toBeNull();
    expect(rateWindowRiskNoteOf(null)).toBeNull();
  });
});

describe('defaultRiskSeverity (#2303)', () => {
  const { defaultRiskSeverity } = require('../reportFacts');
  const order = ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'];

  it('opens on the highest severity that has an open risk', () => {
    expect(defaultRiskSeverity(null, { MEDIUM: [{}, {}], LOW: [{}] }, order)).toBe('MEDIUM');
    expect(defaultRiskSeverity(null, { HIGH: [{}], MEDIUM: [{}] }, order)).toBe('HIGH');
    expect(defaultRiskSeverity(null, { CRITICAL: [{}] }, order)).toBe('CRITICAL');
  });

  it('reads the stored count when a severity shows nothing but the facts say it has risks', () => {
    const synthesis = { facts: { open_risks_by_severity: { HIGH: 2 } } };
    expect(defaultRiskSeverity(synthesis, {}, order)).toBe('HIGH');
  });

  it('keeps the first tab when no severity has an open risk', () => {
    expect(defaultRiskSeverity(null, {}, order)).toBe('CRITICAL');
    expect(defaultRiskSeverity(null, { LOW: [] }, order)).toBe('CRITICAL');
  });

  it('opens on UNRATED when the open risks are all unrated', () => {
    const tabs = [...order, 'UNRATED'];
    expect(defaultRiskSeverity(null, { UNRATED: [{}] }, tabs)).toBe('UNRATED');
    expect(defaultRiskSeverity(null, { LOW: [{}], UNRATED: [{}] }, tabs)).toBe('LOW');
  });

  it('skips an empty higher severity', () => {
    expect(defaultRiskSeverity(null, { CRITICAL: [], HIGH: [], MEDIUM: [{}] }, order)).toBe('MEDIUM');
  });
});

describe('the cost and assumptions helpers (#2316)', () => {
  const t = (key) => key;

  test('the caveat is split around the calculator address, and a custom caveat is left whole', () => {
    const parts = costCaveatPartsOf({ facts: { cost_caveat: 'Check prices in the AWS Pricing Calculator (https://calculator.aws/).' } });
    expect(parts).toEqual({ before: 'Check prices in the ', label: 'AWS Pricing Calculator', href: 'https://calculator.aws/', after: '.' });
    expect(costCaveatPartsOf({ facts: { cost_caveat: 'Prices are estimates.' } })).toEqual({ before: 'Prices are estimates.', label: '', href: '', after: '' });
    expect(costCaveatPartsOf({}).href).toBe('https://calculator.aws/');
  });

  test('the facts supply the role, the price list, the growth lines and the assumption items', () => {
    const synthesis = { facts: {
      engines: [{ engine: 'dynamodb', role: 'Migration target' }],
      price_list: { region: 'us-east-1' },
      growth_scenarios: { lines: ['Cost at 5× growth: $1', ''] },
      assumptions: { assumptions: [{ key: 'growth', setting: 5, origin: 'default', line: 'Growth: 5×' }, { bad: true }] },
    } };
    expect(engineRoleOf(synthesis, 'dynamodb')).toBe('Migration target');
    expect(engineRoleOf(synthesis, 'redis')).toBe('');
    expect(priceListOf(synthesis)).toEqual({ region: 'us-east-1' });
    expect(growthScenarioLinesOf(synthesis)).toEqual(['Cost at 5× growth: $1']);
    expect(assumptionItemsOf(synthesis)).toHaveLength(1);
    expect(assumptionItemsOf({})).toEqual([]);
    expect(priceListOf({})).toBeNull();
  });

  test('a value is formatted from the setting, never counted', () => {
    expect(assumptionValueText(t, { key: 'growth', setting: 5 })).toBe('5×');
    expect(assumptionValueText(t, { key: 'peak', setting: 2.5 })).toBe('2.5×');
    expect(assumptionValueText(t, { key: 'peak', setting: null })).toBe('analysis-results.assumptions-panel.value.not-measured');
    expect(assumptionValueText(t, { key: 'pricing', setting: 'us-east-1' })).toBe('us-east-1');
    expect(assumptionValueText(t, { key: 'rate_window', setting: 3600, basis: 'uptime' })).toBe('analysis-results.assumptions-panel.value.rate-uptime');
    expect(assumptionValueText(t, { key: 'rate_window', setting: 3600, basis: null })).toBe('');
    expect(assumptionValueText(t, { key: 'rate_window_check', setting: 'representative' })).toBe('analysis-results.assumptions-panel.value.window-representative');
  });

  test('the cost table is the facts engines and their total, or null before them', () => {
    const facts = { engines: [{ engine: 'dynamodb', monthly_cost_usd: 1 }, null], engine_cost_total_usd: 1 };
    expect(costTableOf({ facts })).toEqual({ rows: [{ engine: 'dynamodb', monthly_cost_usd: 1 }], total: 1 });
    expect(costTableOf({ facts: {} })).toBeNull();
    expect(costTableOf(null)).toBeNull();
  });

  test('the sized-for cell names the basis only, never repeating the queries or the rate', () => {
    const t2 = (key, o = {}) => `${key}${o.formatted ? `|${o.formatted}` : ''}`;
    expect(costSizingBasisText(t2, { basis: 'assigned_queries', sized_query_count: 67, calls_per_second: 9 }))
      .toBe('analysis-results.cost-breakdown.basis.assigned_queries');
    expect(costSizingBasisText(t2, { basis: 'assigned_queries', unqueried_table_count: 28 }))
      .toBe('analysis-results.cost-breakdown.basis.assigned_queries' + 'analysis-results.cost-breakdown.sized-unqueried|28');
    expect(costSizingBasisText(t2, null)).toBe('');
  });
});

// #2484: the objects each Aurora design leaves out, read as the facts state them.
describe('designLeftOutOf', () => {
  test('reads facts.design_left_out, and an empty list without it', () => {
    const rows = [{ engine: 'aurora_postgresql', object_count: 2, objects: [{ name: 'a' }, { name: 'b' }] }];
    expect(designLeftOutOf({ facts: { design_left_out: rows } })).toEqual(rows);
    expect(designLeftOutOf({ facts: { design_left_out: null } })).toEqual([]);
    expect(designLeftOutOf({ facts: {} })).toEqual([]);
    expect(designLeftOutOf(null)).toEqual([]);
    expect(designLeftOutOf({ facts: { design_left_out: [null, { objects: [] }] } })).toEqual([]);
  });
});

describe('designChangesOf (#2289)', () => {
  test('reads facts.design_changes, and an empty list without it', () => {
    const rows = [{ engine: 'aurora_mysql', table_count: 1, changed_table_count: 0, change_counts: {}, table_changes: [] }];
    expect(designChangesOf({ facts: { design_changes: rows } })).toEqual(rows);
    expect(designChangesOf({ facts: { design_changes: null } })).toEqual([]);
    expect(designChangesOf({ facts: {} })).toEqual([]);
    expect(designChangesOf({ facts: { design_changes: [null, { table_changes: [] }] } })).toEqual([]);
  });
});
