import { memo } from 'react';
import Box from "@cloudscape-design/components/box";
import { isCacheEngine } from "../utils/cacheLayer";
import { CACHE_FIT_DEFINITION, CACHE_FIT_LABEL } from "../utils/workloadFitLabels";

/**
 * The pattern analysis page's average figure for one engine (#1291). An owning
 * engine keeps "Avg workload fit"; the cache layer owns no queries, so its figure is
 * labelled "Avg cache fit" and carries the shared cache fit definition, the same
 * sentence the reports print. Callers pass `t` (the page's `useTranslation` function).
 */
const EngineFitFigure = ({ target, value, t }) => {
  const cache = isCacheEngine(target);
  return (
    <Box>
      <Box variant="awsui-key-label">
        {cache
          ? t('engine-analysis.summary.avg-cache-fit', { fit: CACHE_FIT_LABEL.toLowerCase() })
          : t('engine-analysis.summary.avg-confidence')}
      </Box>
      <Box fontSize="heading-xl" fontWeight="bold">{value}%</Box>
      {cache && (
        <Box fontSize="body-s" color="text-status-inactive">{CACHE_FIT_DEFINITION}</Box>
      )}
    </Box>
  );
};

export default memo(EngineFitFigure);
