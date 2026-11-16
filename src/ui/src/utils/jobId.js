// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// What the API accepts as a job id: 1 to 128 ASCII letters, digits, underscores
// and hyphens (JOB_ID_PATTERN in src/shared/job_id.py). Any other id cannot name
// an assessment. utils/__tests__/jobId.test.js fails if the two drift apart.
export const JOB_ID_SHAPE = /^[A-Za-z0-9_-]{1,128}$/;

export function isWellFormedJobId(jobId) {
  return JOB_ID_SHAPE.test(jobId || '');
}
