// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { memo } from 'react';
import Alert from "@cloudscape-design/components/alert";
import Box from "@cloudscape-design/components/box";
import SpaceBetween from "@cloudscape-design/components/space-between";
import { dynamodbMergeReviewsOf } from "../utils/reportFacts";

/**
 * "DynamoDB merge-review decisions" (#1780): for each source table two or more design
 * groups each gave its own home, the authoritative home the DynamoDB group merge chose
 * and the derived copies kept in sync from its DynamoDB Streams, from report.json's
 * facts.dynamodb_merge_reviews -- the same lines the engineering report lists and the
 * same sentences the executive report states (src/report/merge_reviews.py). It only
 * formats the facts (ADR-032).
 *
 * Callers pass `t` (the page's own `useTranslation` function).
 */
const DynamoDBMergeReviewsCallout = memo(({ synthesis, t }) => {
  const f = dynamodbMergeReviewsOf(synthesis);
  if (!f) return null;
  return (
    <div id="dynamodb-merge-reviews">
      <Alert type="info" header={t('analysis-results.merge-reviews.header')}>
        <SpaceBetween size="s">
          <Box><span data-figure="merge-review-summary">{f.summary_line}</span></Box>
          <Box><span data-figure="merge-review-review">{f.review_line}</span></Box>
          {f.decisions.map((d, i) => (
            <Box key={`${i}:${d.authoritative_home}`}>
              <div data-figure="merge-review-decision">{d.line}</div>
            </Box>
          ))}
        </SpaceBetween>
      </Alert>
    </div>
  );
});

export default DynamoDBMergeReviewsCallout;
