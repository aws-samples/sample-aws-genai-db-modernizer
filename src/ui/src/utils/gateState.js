// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { awaitsApproval } from './resultsLinks';

// #2326: where the assignment gate stands, read from what the API returns (the job's
// stages and status, the assignment version and status, whether an approval is recorded,
// the version the results were written for). The page only words it; the approval itself
// is recorded by the existing resume route.
//
//   unknown   no such job (nothing to decide)
//   waiting   the assessment waits at the gate: the only state that offers a decision
//   failed    the job failed; the page links its progress
//   running   no approval is recorded yet and the job is not waiting (a stage list that
//             has not reached the gate): neutral, never "Approved"
//   rerouted  the version shown is the post-schema router's, after the customer approved
//             the version before it; never stated as approved
//   later     the assignment shown is newer than the version the results were built on
//   done      approved, and the results exist
//   approved  approved, the assessment is still running
// Terminal statuses that never reach results: the API's FAILED and CANCELLED (assessments.py).
const FAILED = new Set(['FAILED', 'CANCELLED', 'failed', 'cancelled', 'error']);

// The approval stage completed in the job's stage list (the cloud history records it).
function gateStageCompleted(job) {
  const stages = job?.progress?.stages;
  return Array.isArray(stages)
    && stages.some((st) => st?.name === 'WaitForAssignmentApproval' && st?.status === 'completed');
}

export function gateState({
  job,
  assignmentVersion,
  assignmentStatus,
  previousVersion,
  approvalRecorded,
  resultsVersion,
  resultsExist,
}) {
  if (!job || !job.database_name) return { kind: 'unknown', version: null };
  const version = Number.isInteger(assignmentVersion) ? assignmentVersion : null;
  if (awaitsApproval(job)) return { kind: 'waiting', version };
  if (FAILED.has(job.status)) return { kind: 'failed', version };
  const results = Number.isInteger(resultsVersion) ? resultsVersion : null;
  // Results exist: the job is past the gate, as the API reads it, even when a job saved before
  // approvals were recorded has no approval record.
  if (!(approvalRecorded === true || gateStageCompleted(job) || results !== null)) return { kind: 'running', version };
  if (assignmentStatus === 'rerouted_after_approval') {
    return {
      kind: 'rerouted',
      version,
      approvedVersion: Number.isInteger(previousVersion) ? previousVersion : null,
      resultsCurrent: !!resultsExist && results !== null && results === version,
    };
  }
  if (results !== null && version !== null && version > results) {
    return { kind: 'later', version, resultsVersion: results };
  }
  if (resultsExist) return { kind: 'done', version };
  return { kind: 'approved', version };
}

// Only a job waiting at the gate offers approval and overrides.
export const offersDecision = (state) => state.kind === 'waiting';
