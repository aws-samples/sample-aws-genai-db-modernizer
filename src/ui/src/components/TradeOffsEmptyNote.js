// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { memo } from 'react';
import Box from "@cloudscape-design/components/box";

/**
 * #2324: an engine tab under "Trade-offs and design decisions" with no trade-off left
 * (synthesis drops a placeholder one) says so, by the engine's catalog name. Nothing
 * when the tab lists any.
 */
const TradeOffsEmptyNote = memo(({ engine, tradeOffs, engineLabels, t }) => {
  if (Array.isArray(tradeOffs) && tradeOffs.length > 0) return null;
  return (
    <div className="trade-offs-empty">
      <Box color="text-body-secondary">
        {t('analysis-results.tradeoffs.empty', { engine: (engineLabels && engineLabels[engine]) || engine })}
      </Box>
    </div>
  );
});

export default TradeOffsEmptyNote;
