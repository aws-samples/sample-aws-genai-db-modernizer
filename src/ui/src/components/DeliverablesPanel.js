// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { memo } from 'react';
import Box from "@cloudscape-design/components/box";
import SpaceBetween from "@cloudscape-design/components/space-between";
import StatusIndicator from "@cloudscape-design/components/status-indicator";
import Button from "@cloudscape-design/components/button";

// A file size for display: "512 B", "20 KB", "1.2 MB".
export function formatBytes(n) {
  if (typeof n !== 'number' || !Number.isFinite(n)) return '';
  if (n < 1024) return `${n} B`;
  const units = ['KB', 'MB', 'GB'];
  let value = n / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const shown = value >= 10 ? Math.round(value) : Math.round(value * 10) / 10;
  return `${shown} ${units[unit]}`;
}

/**
 * The deliverable buttons at the top of the results page (#957, #1028):
 * Executive Summary, Engineering Report and Assessment Detail, each the
 * download of one file of the job's deliverables/ folder (#955), as GET
 * /assessments/{job_id}/deliverables lists it (the folder's own definition,
 * FOLDER_ENTRIES in src/report/deliverables.py). A file the folder does not
 * hold has no button; index.md stays in the folder, without a button.
 *
 * Callers pass the listing (`files`, `loading`, `error`), the API base URL
 * and `t` (the page's own `useTranslation` function), like QueryRoutingTable.
 */

// One entry per button, by FOLDER_ENTRIES filename (#1020: the executive report PDF).
export const DELIVERABLE_BUTTONS = [
  { id: 'executive-summary', filename: 'executive-report.pdf', variant: 'primary' },
  { id: 'engineering-report', filename: 'engineering-report.md', variant: 'normal' },
  { id: 'assessment-detail', filename: 'assessment-data.json', variant: 'normal' },
];

// Literal keys, so the translation test sees each one.
function buttonLabel(id, t) {
  if (id === 'executive-summary') return t('analysis-results.deliverables.button.executive-summary');
  if (id === 'engineering-report') return t('analysis-results.deliverables.button.engineering-report');
  return t('analysis-results.deliverables.button.assessment-detail');
}

const DeliverablesPanel = memo(({ files, loading, error, baseUrl, t }) => {
  const byName = new Map((files || []).map(f => [f.filename, f]));
  const buttons = DELIVERABLE_BUTTONS.filter(b => byName.has(b.filename));
  let body;
  if (loading) {
    body = <StatusIndicator type="loading">{t('analysis-results.deliverables.loading')}</StatusIndicator>;
  } else if (error) {
    body = <StatusIndicator type="error">{t('analysis-results.deliverables.error', { error })}</StatusIndicator>;
  } else if (buttons.length === 0) {
    body = <Box color="text-body-secondary">{t('analysis-results.deliverables.empty')}</Box>;
  } else {
    body = (
      <SpaceBetween direction="horizontal" size="xs">
        {buttons.map(({ id, filename, variant }) => {
          const file = byName.get(filename);
          return (
            <span
              key={filename}
              data-deliverable={filename}
              title={`${file.title} · ${filename} · ${formatBytes(file.size_bytes)}`}
            >
              <Button
                variant={variant}
                iconName="download"
                href={`${baseUrl}${file.path}`}
                download={filename}
                ariaLabel={t('analysis-results.deliverables.download-label', { filename })}
              >
                {buttonLabel(id, t)}
              </Button>
            </span>
          );
        })}
      </SpaceBetween>
    );
  }
  return <div id="deliverables-section">{body}</div>;
});

export default DeliverablesPanel;
