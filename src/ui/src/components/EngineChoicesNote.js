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
// #2147: "2 tables and 1 statement with no source table", from the facts' split.
function choiceScope(tables, noTable, t) {
  const statements = t('analysis-results.choice-no-table-statements', { count: noTable });
  if (!tables) return statements;
  return t('analysis-results.choice-tables-and', {
    tables: t('analysis-results.choice-tables', { count: tables }),
    statements,
  });
}

const EngineChoicesNote = memo(({ synthesis, t }) => {
  const n = engineChoiceCountsOf(synthesis);
  if (!n.pendingTables && !n.decidedTables) return null;
  return (
    <div id="engine-choices-note">
      {n.pendingTables > 0 && (
        <Alert type="warning">
          {n.pendingNoTable > 0
            ? t('analysis-results.pending-choices-split', {
                count: n.pendingTables,
                what: choiceScope(n.pendingTableChoices, n.pendingNoTable, t),
                queries: t('analysis-results.choice-queries', { count: n.pendingQueries }),
              })
            : t('analysis-results.pending-choices', {
                count: n.pendingTables,
                queries: t('analysis-results.choice-queries', { count: n.pendingQueries }),
              })}
        </Alert>
      )}
      {n.decidedTables > 0 && (
        <Alert type="info">
          {n.decidedNoTable > 0
            ? t('analysis-results.auto-decided-choices-split', {
                what: choiceScope(n.decidedTableChoices, n.decidedNoTable, t),
                queries: t('analysis-results.choice-queries', { count: n.decidedQueries }),
              })
            : t('analysis-results.auto-decided-choices', {
                count: n.decidedTables,
                queries: t('analysis-results.choice-queries', { count: n.decidedQueries }),
              })}
        </Alert>
      )}
    </div>
  );
});

export default EngineChoicesNote;
