// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { memo } from 'react';
import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import SpaceBetween from "@cloudscape-design/components/space-between";
import { dynamodbMergeConflictsOf } from "../utils/reportFacts";

/**
 * "Access patterns the merged DynamoDB design does not serve" (#802): each failure the
 * DynamoDB group merge left unresolved, with the access patterns and queries the merged
 * design does not serve, from report.json's facts.dynamodb_merge_conflicts -- the same
 * list the engineering report prints and the same totals the executive report states
 * (src/report/merge_conflicts.py). It only formats the facts (ADR-032).
 *
 * Callers pass `t` (the page's own `useTranslation` function).
 */

// The same line as the engineering report's (src/report/merge_conflicts.py): what the
// patterns are, the patterns (one an earlier conflict lists says so), the table they
// run on and their queries, each part when it has one.
const LABELS = {
  unserved: 'analysis-results.merge-conflicts.label-unserved',
  at_risk: 'analysis-results.merge-conflicts.label-at_risk',
};

function conflictLine(conflict, t) {
  const patterns = conflict.pattern_ids || [];
  const label = LABELS[conflict.status];
  if (!label || patterns.length === 0) {
    return t(conflict.status === 'not_recorded'
      ? 'analysis-results.merge-conflicts.not-recorded'
      : 'analysis-results.merge-conflicts.none-named');
  }
  const repeated = conflict.repeated_pattern_ids || [];
  const shown = patterns
    .map(id => (repeated.includes(id) ? t('analysis-results.merge-conflicts.also-listed', { id }) : id))
    .join(', ');
  const queries = conflict.query_ids || [];
  const keys = conflict.table_name
    ? ['analysis-results.merge-conflicts.patterns-on', 'analysis-results.merge-conflicts.patterns-on-queries']
    : ['analysis-results.merge-conflicts.patterns', 'analysis-results.merge-conflicts.patterns-queries'];
  return t(keys[queries.length > 0 ? 1 : 0], {
    label: t(label), patterns: shown, table: conflict.table_name, queries: queries.join(', '),
  });
}

const DynamoDBMergeConflictsCallout = memo(({ synthesis, t }) => {
  const f = dynamodbMergeConflictsOf(synthesis);
  if (!f) return null;
  return (
    <div id="dynamodb-merge-conflicts">
      <Alert type="warning" header={t('analysis-results.merge-conflicts.header')}>
        <SpaceBetween size="s">
          <Box><span data-figure="merge-conflict-summary">{f.summary_line}</span></Box>
          <Box>{t('analysis-results.merge-conflicts.intro')}</Box>
          {f.conflicts.map((c, i) => (
            // Two failures can read the same (two patterns with no ID on one missing
            // table): the position keeps their keys apart.
            <Box key={`${i}:${c.failure}`}>
              <div data-figure="merge-failure">{c.failure}</div>
              <div data-figure="merge-unserved">{conflictLine(c, t)}</div>
            </Box>
          ))}
        </SpaceBetween>
      </Alert>
    </div>
  );
});

export default DynamoDBMergeConflictsCallout;
