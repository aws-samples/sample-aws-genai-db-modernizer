// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// The Dashboard's assessments list (#2305): a row is named by its database,
// and a finished job opens on its results rather than its progress page.

const FINISHED_WITH_RESULTS = new Set(['SUCCEEDED', 'COMPLETED']);

export function assessmentHref(jobId, status) {
  return FINISHED_WITH_RESULTS.has(status)
    ? `/analysis/results/${jobId}`
    : `/analysis/monitor/summary/${jobId}`;
}

export function databaseLabel(databaseName, jobId) {
  return databaseName || jobId;
}

// The accessible name of a row's copy button (#2340 review): the database and the
// id, so a screen reader can tell rows apart, even two jobs on one database.
export function copyJobIdLabel(t, databaseName, jobId) {
  return t('common.actions.copy-job-id-for', { name: databaseLabel(databaseName, jobId), id: jobId });
}
