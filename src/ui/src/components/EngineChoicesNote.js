// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { memo } from 'react';
import Alert from "@cloudscape-design/components/alert";
import { engineChoiceCountsOf } from "../utils/reportFacts";

/**
 * #1003: the results page's lines for the engine choices -- the same ones the
 * engineering report and the PDF state, from report.json's facts: the tables
 * whose queries still tie between engines (a choice for the customer), and the
 * choices the decide-for-me rule settled (decided automatically, review if needed).
 */
const EngineChoicesNote = memo(({ synthesis, t }) => {
  const n = engineChoiceCountsOf(synthesis);
  if (!n.pendingTables && !n.decidedTables) return null;
  return (
    <div id="engine-choices-note">
      {n.pendingTables > 0 && (
        <Alert type="warning">
          {t('analysis-results.pending-choices', {
            count: n.pendingTables,
            queries: t('analysis-results.choice-queries', { count: n.pendingQueries }),
          })}
        </Alert>
      )}
      {n.decidedTables > 0 && (
        <Alert type="info">
          {t('analysis-results.auto-decided-choices', {
            count: n.decidedTables,
            queries: t('analysis-results.choice-queries', { count: n.decidedQueries }),
          })}
        </Alert>
      )}
    </div>
  );
});

export default EngineChoicesNote;
