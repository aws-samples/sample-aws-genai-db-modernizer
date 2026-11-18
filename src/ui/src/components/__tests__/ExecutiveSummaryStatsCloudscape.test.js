// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2315 (#2458 review): the five figures render with the real Cloudscape components and
 * Cloudscape warns about nothing. KeyValuePairs supports four columns at most and warns
 * in a development build when given five; the row is a five-column ColumnLayout with a
 * minimum column width instead, each cell a KeyValuePairs of one item.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { createInstance } from 'i18next';
import en from '../../locales/en.json';
import ExecutiveSummaryStats from '../ExecutiveSummaryStats';
import { costBaselineStats } from '../../utils/tcoAnalysis';
import { executiveSummaryOf } from '../../utils/reportFacts';

global.IS_REACT_ACT_ENVIRONMENT = true;

const FACTS = {
  open_risk_count: 9,
  critical_high_risk_count: 0,
  migration_wave_count: 3,
  query_total_count: 107,
  source_table_count: 50,
  executive_brief: {
    overall_risk_level: 'MEDIUM',
    decision_wave: 1,
    waves: [
      { wave: 1, engines: ['aurora_mysql'], kind: 'homogeneous' },
      { wave: 2, engines: ['elasticache'], kind: 'cache' },
      { wave: 3, engines: ['dynamodb'], kind: 'migration' },
    ],
  },
};

test('the five figures render with real Cloudscape components and no Cloudscape warning', async () => {
  const i18n = createInstance();
  await i18n.init({
    resources: { en: { translation: en } },
    lng: 'en',
    keySeparator: false,
    interpolation: { escapeValue: false },
  });
  const warn = jest.spyOn(console, 'warn').mockImplementation(() => {});
  const error = jest.spyOn(console, 'error').mockImplementation(() => {});
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  try {
    act(() => {
      root.render(
        <ExecutiveSummaryStats
          t={i18n.t.bind(i18n)}
          summary={executiveSummaryOf({ facts: FACTS })}
          targetEngines={<span>Aurora MySQL</span>}
          cacheLayerLine="Cache layer · 19 designed cached reads · 82.1% of calls"
          cacheChange="The cache layer covers 19 of the 20 cache candidates the gate listed."
          targetEngineNotes={['ElastiCache is recommended as a cache-aside layer.']}
          projectedCost={507.39}
          costBaseline={costBaselineStats({ current_monthly_cost: 0, current_cost_known: false })}
        />,
      );
    });
    const tiles = [...container.querySelectorAll('[data-tile]')].map(el => el.getAttribute('data-tile'));
    expect(tiles).toEqual(['projected-cost', 'overall-risk', 'waves', 'queries', 'target-engines']);
    const calls = [...warn.mock.calls, ...error.mock.calls].map(args => args.join(' '));
    expect(calls.filter(text => /AwsUi|Cloudscape|KeyValuePairs|ColumnLayout/.test(text))).toEqual([]);
  } finally {
    act(() => root.unmount());
    container.remove();
    warn.mockRestore();
    error.mockRestore();
  }
});
