// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { memo } from 'react';
import Alert from "@cloudscape-design/components/alert";
import { unreviewedMoveCountOf } from "../utils/reportFacts";

/**
 * #746: one plain line when the reality check's model review failed for some
 * consolidation moves, so they stand unreviewed -- the same line the
 * engineering report and the PDF print, from report.json's
 * facts.unreviewed_move_count. It only formats the fact (ADR-032).
 */
const UnreviewedMovesNote = memo(({ synthesis, t }) => {
  const n = unreviewedMoveCountOf(synthesis);
  if (!n) return null;
  return (
    <div id="unreviewed-moves-note">
      <Alert type="info">{t('analysis-results.unreviewed-moves', { count: n })}</Alert>
    </div>
  );
});

export default UnreviewedMovesNote;
