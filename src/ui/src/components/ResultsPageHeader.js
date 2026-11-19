// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import CopyToClipboard from '@cloudscape-design/components/copy-to-clipboard';
import Header from '@cloudscape-design/components/header';
import { copyJobIdLabel } from '../utils/assessmentRows';
import { formatReportDate } from '../utils/format';

/**
 * The results page breadcrumb (#2314): "Assessments > wordpress". The assessments list
 * is the dashboard; the last crumb is this page, named for its database (the id until
 * the name is known).
 */
export function resultsBreadcrumbs(t, databaseName, jobId) {
  return [
    { href: '/dashboard', text: t('analysis-results.breadcrumb.assessments') },
    { href: `/analysis/results/${jobId}`, text: databaseName || jobId },
  ];
}

/**
 * The results page h1 (#2314): "wordpress assessment", so each open tab names its
 * assessment. The description states the source engine (the label the API gives the
 * assessment, the one the assessments list and the progress page show), the
 * assessment id with a copy button, and the date the report was written (the
 * executive report cover's date). A part the job has no record of is left out.
 */
export default function ResultsPageHeader({ databaseName, sourceLabel, jobId, timestamp, actions, t }) {
  const date = formatReportDate(timestamp);
  // Header puts the description in a <p>, so every part is inline (spans only).
  const parts = [];
  if (sourceLabel) {
    parts.push(<span key="source">{t('analysis-results.header.source', { source: sourceLabel })}</span>);
  }
  parts.push(
    <span key="id">
      {t('analysis-results.header.assessment-id')}{' '}
      <CopyToClipboard
        variant="inline"
        textToCopy={jobId}
        copyButtonAriaLabel={copyJobIdLabel(t, databaseName, jobId)}
        copySuccessText={t('common.success.copied')}
        copyErrorText={t('common.errors.copy-failed')}
      />
    </span>,
  );
  if (date) parts.push(<span key="date">{date}</span>);
  return (
    <div id="results-header">
      <Header
        variant="h1"
        actions={actions}
        description={parts.flatMap((part, i) => (i === 0 ? [part] : [<span key={`sep-${i}`} aria-hidden="true">{' | '}</span>, part]))}
      >
        {databaseName
          ? t('analysis-results.header.title', { database: databaseName })
          : t('analysis-results.header.title-unnamed')}
      </Header>
    </div>
  );
}
