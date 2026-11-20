// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { memo } from 'react';
import Box from "@cloudscape-design/components/box";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import KeyValuePairs from "@cloudscape-design/components/key-value-pairs";
import Link from "@cloudscape-design/components/link";
import Popover from "@cloudscape-design/components/popover";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import { displayEngine } from '../utils/engineNames';
import { formatCount, formatMoney, formatMonthlyMoney } from '../utils/format';
import { riskSeverityLabel, riskSeverityStatus } from '../utils/riskAssessment';

/**
 * The executive summary's five figures on the results page (#2315, after #1028): one
 * row, one headline figure per item with its unit, in this order: Monthly cost, Overall
 * risk, Waves, Queries, Target engines. KeyValuePairs lays out four columns at most, so
 * the row is a five-column ColumnLayout with a minimum column width (it wraps on a narrow
 * screen), each cell a KeyValuePairs of one item. It only formats the figures it is
 * given (ADR-032): `summary` is executiveSummaryOf(synthesis) (utils/reportFacts.js), every
 * figure read from report.json's facts.
 *
 * - Monthly cost: the projected cost (0 included; only a missing figure shows a dash),
 *   with the cost scope label, the list prices' source, region and date (`priceList`,
 *   facts.price_list, #1196), the queries it leaves out (#1329), the current cost and
 *   saving when the cost baseline is known (costBaselineStats in utils/tcoAnalysis.js), and
 *   each retired engine the customer runs today with its counted saving (#1076).
 * - Overall risk: the level as a StatusIndicator, with the open and the critical or high
 *   risk counts.
 * - Waves: the wave count, with each wave's engines in order.
 * - Queries: every collected query, with the source tables, and the queries no engine in
 *   the customer's engine scope serves (`noEngineInScope`, #1329).
 * - Target engines: the page's engine badges (`targetEngines`) and the cache layer's line
 *   (`cacheLayerLine`), the cache change as a warning (`cacheChange`, #1252: the executive
 *   report prints it in its body, so the summary shows it too), the customer's engine
 *   scope (#1328), and an Info link whose popover holds the longer notes
 *   (`targetEngineNotes`: the cache layer's role, the routines' temporary objects).
 *
 * The query patterns and the access-pattern counts are on the Queries tab.
 */
// The price date(s) as the facts layer stated them; it only formats (ADR-032).
// A retired engine's reason as the PDF and the engineering report word it (src/report/formatting.py
// _retired_in_use_lines): trimmed, one closing period, nothing when it is empty (#1127).
function retiredReason(row) {
  const reason = String(row.reason ?? '').trim().replace(/\.+$/, '');
  return reason ? `${reason}.` : '';
}

export function priceListLine(t, priceList) {
  if (!priceList) return null;
  const dates = Object.entries(priceList.engine_dates || {});
  let when = priceList.retrieved_on;
  if (!when && dates.length === 0) return null;
  if (!when) when = dates.map(([engine, date]) => `${date} (${displayEngine(engine)})`).join(', ');
  const undated = (priceList.undated_engines || []).map(displayEngine).join(', ');
  return t('analysis-results.executive-summary.price-list', {
    source: priceList.source,
    region: priceList.region,
    pricingModel: priceList.pricing_model,
    date: when,
  }) + (undated ? t('analysis-results.executive-summary.price-list-undated', { engines: undated }) : '');
}

// The narrowest a figure's column gets before the row wraps (px).
const SUMMARY_COLUMN_MIN_WIDTH = 170;

function Note({ figure, children }) {
  return (
    <Box fontSize="body-s" color="text-body-secondary">
      {figure ? <span data-figure={figure}>{children}</span> : children}
    </Box>
  );
}

function Headline({ figure, children }) {
  return <Box fontSize="heading-l" fontWeight="bold"><span data-figure={figure}>{children}</span></Box>;
}

function riskCountsLine(t, summary) {
  if (summary.openRiskCount === null) return null;
  const open = t('analysis-results.executive-summary.open-risks', { count: summary.openRiskCount });
  if (summary.criticalHighRiskCount === null || summary.openRiskCount === 0) return open;
  return summary.criticalHighRiskCount > 0
    ? t('analysis-results.executive-summary.open-risks-critical-high', { open, count: summary.criticalHighRiskCount })
    : t('analysis-results.executive-summary.open-risks-none-critical-high', { open });
}

const ExecutiveSummaryStats = memo(({
  t, summary, targetEngines, cacheLayerLine, cacheChange, targetEngineNotes, projectedCost, costBaseline,
  retiredEngines, engineScope, noEngineInScope, priceList, costScopeLabel,
}) => {
  const baselineKnown = Boolean(costBaseline && costBaseline.known);
  // Missing (null, as resolveCostBreakdown reports it) shows a dash; a real $0.00
  // projection is a figure and shows.
  const hasProjectedCost = typeof projectedCost === 'number' && Number.isFinite(projectedCost);
  const retiredList = Array.isArray(retiredEngines) ? retiredEngines : [];
  const priceLine = priceListLine(t, priceList);
  const notes = (targetEngineNotes || []).filter(Boolean);
  const riskLine = riskCountsLine(t, summary);
  const waveLine = summary.waveEngines
    .map(engines => engines.map(displayEngine).join(' + '))
    .filter(Boolean)
    .join(', ');
  const noEngine = noEngineInScope && noEngineInScope.count > 0 ? noEngineInScope : null;

  const items = [
    {
      label: t('analysis-results.executive-summary.monthly-cost'),
      value: (
        <div data-tile="projected-cost">
          <Headline figure="projected-cost">{hasProjectedCost ? formatMonthlyMoney(projectedCost) : '—'}</Headline>
          {costScopeLabel && <Note figure="cost-scope">{costScopeLabel}</Note>}
          {priceLine && <Note figure="price-list">{priceLine}</Note>}
          {noEngine && (
            <Note figure="cost-excludes-no-engine">
              {t('analysis-results.executive-summary.cost-excludes-no-engine', { count: noEngine.count })}
            </Note>
          )}
          {baselineKnown && (
            <Note figure="current-cost">
              {t('analysis-results.executive-summary.current-cost-line', {
                current: costBaseline.currentCostStat, savings: costBaseline.savingsStat,
              })}
            </Note>
          )}
          {retiredList.map((row) => (
            <div key={row.engine} data-retired-engine={row.engine}>
              <Note>
                {typeof row.monthly_cost_usd === 'number'
                  ? t('analysis-results.executive-summary.retired-engine-saving', {
                    engine: displayEngine(row.engine), cost: formatMoney(row.monthly_cost_usd),
                  })
                  : t(row.no_saving_reason
                    ? 'analysis-results.executive-summary.retired-engine-no-saving-why'
                    : 'analysis-results.executive-summary.retired-engine-no-saving', {
                    engine: displayEngine(row.engine),
                    reason: row.no_saving_reason,
                  })}
                {retiredReason(row) ? ` ${retiredReason(row)}` : ''}
              </Note>
            </div>
          ))}
        </div>
      ),
    },
    {
      label: t('analysis-results.executive-summary.overall-risk'),
      value: (
        <div data-tile="overall-risk">
          <Box fontSize="heading-m" fontWeight="bold">
            <span data-figure="overall-risk" data-level={summary.riskLevel || ''}>
              {summary.riskLevel
                ? <StatusIndicator type={riskSeverityStatus(summary.riskLevel)}>{riskSeverityLabel(summary.riskLevel, t)}</StatusIndicator>
                : '—'}
            </span>
          </Box>
          {riskLine && <Note figure="open-risks">{riskLine}</Note>}
        </div>
      ),
    },
    {
      label: t('analysis-results.executive-summary.waves'),
      value: (
        <div data-tile="waves">
          <Headline figure="wave-count">{summary.waveCount ?? '—'}</Headline>
          {waveLine && <Note figure="wave-engines">{waveLine}</Note>}
        </div>
      ),
    },
    {
      label: t('analysis-results.executive-summary.queries'),
      value: (
        <div data-tile="queries">
          <Headline figure="query-count">
            {summary.queryCount === null ? '—' : formatCount(summary.queryCount)}
          </Headline>
          {summary.sourceTableCount !== null && summary.sourceTableCount > 0 && (
            <Note figure="source-tables">
              {t('analysis-results.executive-summary.across-source-tables', {
                count: summary.sourceTableCount,
                tables: formatCount(summary.sourceTableCount),
              })}
            </Note>
          )}
          {noEngine && (
            <Note figure="no-engine-in-scope">
              {t('analysis-results.executive-summary.no-engine-in-scope', {
                count: noEngine.count,
                placed: noEngine.placed,
                total: noEngine.total,
              })}
            </Note>
          )}
        </div>
      ),
    },
    {
      label: t('analysis-results.executive-summary.target-engines'),
      info: notes.length > 0 ? (
        <Popover
          triggerType="custom"
          size="large"
          header={t('analysis-results.executive-summary.target-engines-info-header')}
          content={(
            <SpaceBetween size="xs">
              {notes.map((note, i) => <Box key={i} variant="p">{note}</Box>)}
            </SpaceBetween>
          )}
        >
          <span data-figure="target-engines-info">
            <Link variant="info" ariaLabel={t('analysis-results.executive-summary.target-engines-info-label')}>
              {t('analysis-results.executive-summary.info')}
            </Link>
          </span>
        </Popover>
      ) : undefined,
      value: (
        <div data-tile="target-engines" id="target-engines">
          {targetEngines}
          {cacheLayerLine && <Note figure="cache-layer-line">{cacheLayerLine}</Note>}
          {cacheChange && (
            <Box fontSize="body-s">
              <span data-figure="cache-change">
                <StatusIndicator type="warning">{cacheChange}</StatusIndicator>
              </span>
            </Box>
          )}
          {Array.isArray(engineScope) && engineScope.length > 0 && (
            <Note figure="engine-scope">
              {t('analysis-results.executive-summary.engine-scope', {
                engines: engineScope.map(displayEngine).join(', '),
              })}
            </Note>
          )}
        </div>
      ),
    },
  ];

  // One row of five on a desktop page; a narrower container drops to an even column
  // count (Cloudscape's flexible ColumnLayout), then to one column on a phone.
  return (
    <ColumnLayout columns={items.length} minColumnWidth={SUMMARY_COLUMN_MIN_WIDTH}>
      {items.map(item => (
        <KeyValuePairs key={item.label} columns={1} items={[item]} />
      ))}
    </ColumnLayout>
  );
});

export default ExecutiveSummaryStats;
