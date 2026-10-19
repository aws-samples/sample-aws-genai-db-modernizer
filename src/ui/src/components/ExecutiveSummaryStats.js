import { memo } from 'react';
import Box from "@cloudscape-design/components/box";
import ColumnLayout from "@cloudscape-design/components/column-layout";
import { displayEngine } from "../utils/engineNames";

/**
 * The executive summary's tiles on the results page (#1028).
 *
 * The access-pattern (query) count sits just below the database name. A cost
 * tile is shown only when its figure is available: the projected cost when the
 * report has one (0 included; only a missing figure hides it), the current monthly cost and the savings when the cost
 * baseline is known (costBaselineStats in utils/tcoAnalysis.js); otherwise the
 * tile is left out instead of saying the figure is missing. The stay-on-source
 * tile (#949) shows what keeping every query on the source-compatible engine
 * costs and the plan's difference from it, when the report has that baseline
 * (stayOnSourceStat in utils/tcoAnalysis.js). It only formats the figures it is
 * given (ADR-032).
 *
 * `accessPatternCount` is the facts layer's owned access-pattern total (0
 * without schema design), `ownerQueryCount` the queries the plan owns and
 * `cachePatternCount` the cache layer's own patterns; `targetEngines` is the
 * page's own engine badges and notes. Callers pass `t` (the page's own
 * `useTranslation` function), like QueryRoutingTable.
 */
const ExecutiveSummaryStats = memo(({
  t, databaseName, accessPatternCount, ownerQueryCount, cachePatternCount,
  targetEngines, projectedCost, costBaseline, stayOnSource,
}) => {
  let patterns = '—';
  if (accessPatternCount > 0) patterns = accessPatternCount;
  else if (ownerQueryCount > 0) patterns = ownerQueryCount;
  const showOfQueries = accessPatternCount > 0 && ownerQueryCount > 0 && accessPatternCount !== ownerQueryCount;
  const baselineKnown = Boolean(costBaseline && costBaseline.known);
  // Missing (null, as resolveCostBreakdown reports it) hides the tile; a real
  // $0.00 projection is a figure and shows.
  const hasProjectedCost = typeof projectedCost === 'number' && Number.isFinite(projectedCost);
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
      {stayOnSource && (
        <div data-tile="stay-on-source">
          <Box variant="awsui-key-label">
            {t('analysis-results.executive-summary.stay-on-source', { engine: displayEngine(stayOnSource.engine) })}
          </Box>
          <Box fontSize="heading-m" fontWeight="bold">{stayOnSource.costStat}</Box>
          <Box fontSize="body-s" color="text-body-secondary" padding={{ top: 'xxs' }}>
            {t(`analysis-results.executive-summary.stay-on-source-${stayOnSource.kind}`, { amount: stayOnSource.differenceStat })}
          </Box>
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
