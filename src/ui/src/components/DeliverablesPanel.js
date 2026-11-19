// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { memo } from 'react';
import Box from "@cloudscape-design/components/box";
import ButtonDropdown from "@cloudscape-design/components/button-dropdown";
import SpaceBetween from "@cloudscape-design/components/space-between";
import RequestError from "./RequestError";

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

// The folder's own index (index.md) stays in the folder, without a menu item.
const FOLDER_INDEX = 'index.md';

/**
 * The files the Download menu offers: every file the deliverables listing names except
 * the folder index, in the listing's order. The listing is GET
 * /assessments/{job_id}/deliverables, which follows FOLDER_ENTRIES in
 * src/report/deliverables.py, the one list of the folder's files; the UI keeps no list
 * of its own, so a file the folder gains or an old job's folder lacks needs no change here.
 */
export function menuFiles(files) {
  return (files || []).filter(f => f && f.filename && f.path && f.filename !== FOLDER_INDEX);
}

/**
 * The results page's one Download menu (#2314, after #957 and #1028), in the page
 * header's actions. Each item is the download of one file of the job's deliverables/
 * folder (#955), named by its FOLDER_ENTRIES title, which states the file type
 * ("Executive report (PDF)"), with the file name and size under it.
 *
 * Callers pass the listing (`files`, `loading`, `error`), the API base URL
 * and `t` (the page's own `useTranslation` function), like QueryRoutingTable.
 */
const DeliverablesPanel = memo(({ files, loading, error, onRetry, baseUrl, t }) => {
  const listed = menuFiles(files);
  const items = listed.map(file => ({
    id: file.filename,
    text: file.title || file.filename,
    secondaryText: [file.filename, formatBytes(file.size_bytes)].filter(Boolean).join(' · '),
    href: `${baseUrl}${file.path}`,
    download: file.filename,
    ariaLabel: t('analysis-results.deliverables.download-label', { filename: file.filename, title: file.title || file.filename }),
    dataAttributes: { deliverable: file.filename },
  }));
  const unavailable = !loading && !error && items.length === 0;
  return (
    <div id="deliverables-section">
      <SpaceBetween direction="horizontal" size="xs" alignItems="center">
        {error && !loading && (
          <RequestError inline failure={error} lead={t('analysis-results.deliverables.error')} onRetry={onRetry} t={t} />
        )}
        <ButtonDropdown
          variant="primary"
          items={items}
          loading={loading}
          loadingText={t('analysis-results.deliverables.loading')}
          disabled={items.length === 0 && !loading}
        >
          {t('analysis-results.deliverables.menu')}
        </ButtonDropdown>
        {unavailable && <Box color="text-body-secondary" fontSize="body-s">{t('analysis-results.deliverables.empty')}</Box>}
      </SpaceBetween>
    </div>
  );
});

export default DeliverablesPanel;
