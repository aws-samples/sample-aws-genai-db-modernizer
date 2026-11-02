// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import Button from '@cloudscape-design/components/button';
import { useTranslation } from 'react-i18next';

/**
 * The results page's way into the experimental graph explorer (#308).
 * Renders nothing unless `enabled` (the server's flag, useGraphExplorerFlag).
 * The results page also renders it only under `enabled &&`, so with the flag
 * off its header actions are exactly what they were.
 */
export default function ExploreGraphButton({ jobId, enabled }) {
  const { t } = useTranslation();
  if (!enabled || !jobId) return null;
  return (
    <Button iconName="share" href={`/analysis/explore/${encodeURIComponent(jobId)}`}>
      {t('graph-explorer.entry')}
    </Button>
  );
}
