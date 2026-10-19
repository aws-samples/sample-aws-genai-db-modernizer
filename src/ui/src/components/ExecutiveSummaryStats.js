import { memo } from 'react';
import Box from "@cloudscape-design/components/box";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import { displayEngine } from '../utils/engineNames';

/**
 * The executive summary's tiles on the results page (#1028).
 *
 * The access-pattern (query) count sits just below the database name. A cost
 * tile is shown only when its figure is available: the projected cost when the
 * report has one (0 included; only a missing figure hides it), the current monthly cost and the savings when the cost
 * baseline is known (costBaselineStats in utils/tcoAnalysis.js); otherwise the
 * tile is left out instead of saying the figure is missing. It only formats
 * the figures it is given (ADR-032).
 *
 * `retiredEngines` is the facts layer's `retired_in_use_engines` (#1076): the engines the
 * customer runs today that the plan retires, each stated once with its counted saving (or
 * "no saving counted") and the consolidation reason; none shows without one.
 *
 * `accessPatternCount` is the facts layer's owned access-pattern total (0
 * without schema design), `ownerQueryCount` the queries the plan owns and
 * `cachePatternCount` the cache layer's own patterns; `targetEngines` is the
 * page's own engine badges and notes. Callers pass `t` (the page's own
 * `useTranslation` function), like QueryRoutingTable. `queryPatterns` is
 * facts.query_pattern_counts as `{ queries, patterns }` (queryPatternCountsOf),
 * or null for a report written before it (#1146): "1,654 queries in 1,045 distinct
 * query patterns", the line the executive and engineering reports print.
 */
const ExecutiveSummaryStats = memo(({
  t, databaseName, accessPatternCount, ownerQueryCount, cachePatternCount,
  targetEngines, projectedCost, costBaseline, retiredEngines, queryPatterns,
}) => {
  let patterns = '—';
  if (accessPatternCount > 0) patterns = accessPatternCount;
  else if (ownerQueryCount > 0) patterns = ownerQueryCount;
  const showOfQueries = accessPatternCount > 0 && ownerQueryCount > 0 && accessPatternCount !== ownerQueryCount;
  const baselineKnown = Boolean(costBaseline && costBaseline.known);
  // Missing (null, as resolveCostBreakdown reports it) hides the tile; a real
  // $0.00 projection is a figure and shows.
  const hasProjectedCost = typeof projectedCost === 'number' && Number.isFinite(projectedCost);
  const retiredList = Array.isArray(retiredEngines) ? retiredEngines : [];
  return (
    <ColumnLayout columns={4} variant="text-grid">
      <div data-tile="database">
        <Box variant="awsui-key-label">{t('analysis-results.executive-summary.database')}</Box>
        <Box fontSize="heading-m" fontWeight="bold"><span data-figure="database-name">{databaseName || '—'}</span></Box>
        <Box variant="awsui-key-label" padding={{ top: 's' }}>{t('analysis-results.executive-summary.access-patterns')}</Box>
        <Box fontSize="heading-m" fontWeight="bold"><span data-figure="access-patterns">{patterns}</span></Box>
        {/* #361: access patterns and the Query flow's query count don't always
            match (relational engines model as tables, not patterns; several
            queries can converge into one pattern) -- say so. */}
        {showOfQueries && (
          <Box fontSize="body-s" color="text-body-secondary" padding={{ top: 'xxs' }}>
            {t('analysis-results.executive-summary.access-patterns-of-queries', { count: ownerQueryCount })}
          </Box>
        )}
        {/* #1146: the queries and the distinct query patterns they make. */}
        {queryPatterns && (
          <Box fontSize="body-s" color="text-body-secondary" padding={{ top: 'xxs' }}>
            <span data-figure="query-patterns">
              {t('analysis-results.executive-summary.query-patterns', {
                count: queryPatterns.patterns,
                patterns: queryPatterns.patterns.toLocaleString('en-US'),
                queries: t('analysis-results.executive-summary.query-patterns-queries', {
                  count: queryPatterns.queries,
                  queries: queryPatterns.queries.toLocaleString('en-US'),
                }),
              })}
            </span>
          </Box>
        )}
        {/* #361: the cache layer's own patterns are real, but never owned
            workload -- called out separately. */}
        {cachePatternCount > 0 && (
          <Box fontSize="body-s" color="text-body-secondary" padding={{ top: 'xxs' }}>
            {t('analysis-results.executive-summary.cache-patterns-note', { count: cachePatternCount })}
          </Box>
        )}
      </div>
      <div data-tile="target-engines">
        <Box variant="awsui-key-label">{t('analysis-results.executive-summary.target-engines')}</Box>
        {targetEngines}
      </div>
      {hasProjectedCost && (
        <div data-tile="projected-cost">
          <Box variant="awsui-key-label">{t('analysis-results.executive-summary.projected-cost')}</Box>
          <Box fontSize="heading-m" fontWeight="bold">{`$${projectedCost.toFixed(2)}/mo`}</Box>
        </div>
      )}
      {retiredList.length > 0 && (
        <div data-tile="retired-engines">
          <Box variant="awsui-key-label">{t('analysis-results.executive-summary.retired-engines')}</Box>
          {retiredList.map((row) => (
            <div key={row.engine} data-retired-engine={row.engine}>
              <Box fontSize="heading-s" fontWeight="bold">
                {typeof row.monthly_cost_usd === 'number'
                  ? t('analysis-results.executive-summary.retired-engine-saving', {
                    engine: displayEngine(row.engine), cost: `$${row.monthly_cost_usd.toFixed(2)}`,
                  })
                  : t(row.no_saving_reason
                    ? 'analysis-results.executive-summary.retired-engine-no-saving-why'
                    : 'analysis-results.executive-summary.retired-engine-no-saving', {
                    engine: displayEngine(row.engine),
                    reason: row.no_saving_reason,
                  })}
              </Box>
              {row.reason && (
                <Box fontSize="body-s" color="text-body-secondary">{`${String(row.reason).replace(/\.+$/, '')}.`}</Box>
              )}
            </div>
          ))}
        </div>
      )}
      {baselineKnown && (
        <div data-tile="current-cost">
          <Box variant="awsui-key-label">{t('analysis-results.executive-summary.current-cost')}</Box>
          <Box fontSize="heading-m" fontWeight="bold">{costBaseline.currentCostStat}</Box>
        </div>
      )}
      {baselineKnown && (
        <div data-tile="savings">
          <Box variant="awsui-key-label">{t('analysis-results.executive-summary.savings')}</Box>
          <Box fontSize="heading-m" fontWeight="bold">{costBaseline.savingsStat}</Box>
        </div>
      )}
    </ColumnLayout>
  );
});

export default ExecutiveSummaryStats;
