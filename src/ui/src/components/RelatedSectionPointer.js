// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { useContext } from "react";
import Box from "@cloudscape-design/components/box";
import Link from "@cloudscape-design/components/link";
import { ResultsSectionNav } from "../hooks/useResultsTabs";
import { dynamodbMergeConflictsOf, dynamodbMergeReviewsOf, unservableOf } from "../utils/reportFacts";

/**
 * #2313 review: the queries that cannot be served as designed are on the Plan tab, the
 * access patterns the merged DynamoDB design does not serve and its merge-review
 * decisions on Risks and decisions. When both sides are there, each points at the
 * other in one line. Only which sections exist is read; nothing is counted (ADR-032).
 */
export function unservedPointers(synthesis) {
  const unservable = !!unservableOf(synthesis);
  const conflicts = !!dynamodbMergeConflictsOf(synthesis);
  const reviews = !!dynamodbMergeReviewsOf(synthesis);
  if (!unservable || !(conflicts || reviews)) return { toRisks: null, toPlan: null };
  return {
    toRisks: conflicts ? 'dynamodb-merge-conflicts' : 'dynamodb-merge-reviews',
    toPlan: 'unservable-section',
  };
}

const TEXT_KEY = {
  'unservable-section': 'analysis-results.related.to-plan',
  'dynamodb-merge-conflicts': 'analysis-results.related.to-risks',
  'dynamodb-merge-reviews': 'analysis-results.related.to-risks',
};

export default function RelatedSectionPointer({ target, t }) {
  const goToSection = useContext(ResultsSectionNav);
  if (!target) return null;
  const follow = (event) => {
    if (!goToSection) return;
    if (event && event.preventDefault) event.preventDefault();
    goToSection(target);
  };
  return (
    <Box fontSize="body-s" color="text-body-secondary">
      <Link href={`#${target}`} onFollow={follow}>{t(TEXT_KEY[target])}</Link>
    </Box>
  );
}
