import { memo } from 'react';
import Box from "@cloudscape-design/components/box";
import { isCacheEngine } from "../utils/cacheLayer";
import { CACHE_FIT_DEFINITION, CACHE_FIT_LABEL } from "../utils/workloadFitLabels";

/**
 * The pattern analysis page's average figure for one engine (#1291). An owning
 * engine keeps "Avg workload fit"; the cache layer owns no queries, so its figure is
 * labelled "Avg cache fit" and carries the shared cache fit definition, the same
 * sentence the reports print. Callers pass `t` (the page's `useTranslation` function).
 *
 * The cache layer's value is the server's routed cache fit, the fit of the reads routed to it
 * (#1325); with no such reads `applicable` is false and the figure reads "Not applicable".
 * The component only displays what it is given.
 */
const EngineFitFigure = ({ target, value, applicable = true, t }) => {
  const cache = isCacheEngine(target);
  return (
    <Box>
      <Box variant="awsui-key-label">
        {cache
          ? t('engine-analysis.summary.avg-cache-fit', { fit: CACHE_FIT_LABEL.toLowerCase() })
          : t('engine-analysis.summary.avg-confidence')}
      </Box>
      <Box fontSize="heading-xl" fontWeight="bold">
        {cache && !applicable ? t('engine-analysis.summary.cache-fit-not-applicable') : `${value}%`}
      </Box>
      {cache && (
        <Box fontSize="body-s" color="text-status-inactive">{CACHE_FIT_DEFINITION}</Box>
      )}
    </Box>
  );
};

export default memo(EngineFitFigure);
