// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { gateState, offersDecision } from '../gateState';

const stage = (status) => ({ progress: { stages: [{ name: 'WaitForAssignmentApproval', status }] }, database_name: 'db' });

test('a job waiting at the gate offers a decision', () => {
  const s = gateState({ job: stage('in-progress'), assignmentVersion: 2 });
  expect(s).toEqual({ kind: 'waiting', version: 2 });
  expect(offersDecision(s)).toBe(true);
});

test('an unknown job offers nothing', () => {
  expect(offersDecision(gateState({ job: {}, assignmentVersion: 1 }))).toBe(false);
  expect(gateState({ job: null }).kind).toBe('unknown');
});

test('an approved job with results is done, without results still approved', () => {
  expect(gateState({ job: stage('completed'), assignmentVersion: 2, resultsVersion: 2, resultsExist: true }))
    .toEqual({ kind: 'done', version: 2 });
  expect(gateState({ job: stage('completed'), assignmentVersion: 2 })).toEqual({ kind: 'approved', version: 2 });
});

test('an assignment newer than the results is a later version, and offers nothing', () => {
  const s = gateState({ job: stage('completed'), assignmentVersion: 3, resultsVersion: 2, resultsExist: true });
  expect(s).toEqual({ kind: 'later', version: 3, resultsVersion: 2 });
  expect(offersDecision(s)).toBe(false);
});

test('a waiting job stays waiting even when an older version has results', () => {
  expect(gateState({ job: stage('in-progress'), assignmentVersion: 3, resultsVersion: 2, resultsExist: true }).kind).toBe('waiting');
});

// A cloud stage history while the reality check runs: the gate is not reached yet.
const cloud = (stages, status = 'RUNNING') => ({ database_name: 'db', status, progress: { stages } });
const preGate = cloud([
  { name: 'RunAssignment', status: 'completed' },
  { name: 'RunRealityCheck', status: 'in-progress' },
]);

test('a job that has not reached the gate is running, never approved', () => {
  const s = gateState({ job: preGate, assignmentVersion: 1 });
  expect(s).toEqual({ kind: 'running', version: 1 });
  expect(offersDecision(s)).toBe(false);
});

test('a job past the gate with no recorded approval and no gate stage is running', () => {
  // Local mode drops the gate stage once passed: only the recorded approval says so.
  const local = cloud([{ name: 'RunRealityCheck', status: 'completed' }]);
  expect(gateState({ job: local, assignmentVersion: 2, approvalRecorded: false }).kind).toBe('running');
  expect(gateState({ job: local, assignmentVersion: 2, approvalRecorded: true }).kind).toBe('approved');
  expect(gateState({ job: local, assignmentVersion: 2, approvalRecorded: true, resultsVersion: 2, resultsExist: true }).kind)
    .toBe('done');
});

test('a cloud history with the gate stage completed is approved without a local record', () => {
  const job = cloud([{ name: 'WaitForAssignmentApproval', status: 'completed' }, { name: 'RunSchemaDesign', status: 'in-progress' }]);
  expect(gateState({ job, assignmentVersion: 2 })).toEqual({ kind: 'approved', version: 2 });
});

test('a failed job is failed, approved or not', () => {
  const job = cloud([{ name: 'WaitForAssignmentApproval', status: 'completed' }, { name: 'RunRefereeSynthesis', status: 'failed' }], 'FAILED');
  expect(gateState({ job, assignmentVersion: 2, approvalRecorded: true })).toEqual({ kind: 'failed', version: 2 });
  expect(gateState({ job: { ...preGate, status: 'FAILED' }, assignmentVersion: 1 }).kind).toBe('failed');
});

test('a post-schema reroute is rerouted after approval of the version before it, never approved', () => {
  const job = cloud([{ name: 'WaitForAssignmentApproval', status: 'completed' }]);
  const base = { job, assignmentVersion: 3, assignmentStatus: 'rerouted_after_approval', previousVersion: 2, approvalRecorded: true };
  expect(gateState(base)).toEqual({ kind: 'rerouted', version: 3, approvedVersion: 2, resultsCurrent: false });
  expect(gateState({ ...base, resultsVersion: 3, resultsExist: true }))
    .toEqual({ kind: 'rerouted', version: 3, approvedVersion: 2, resultsCurrent: true });
});

test('an old saved job with results and no recorded approval is past the gate', () => {
  const job = { database_name: 'db', status: 'SUCCEEDED' };
  expect(gateState({ job, assignmentVersion: 2, approvalRecorded: false, resultsVersion: 2, resultsExist: true }))
    .toEqual({ kind: 'done', version: 2 });
  expect(gateState({ job, assignmentVersion: 3, approvalRecorded: false, resultsVersion: 2, resultsExist: true }).kind)
    .toBe('later');
  expect(gateState({ job, assignmentVersion: 2, approvalRecorded: false }).kind).toBe('running');
});

test('a cancelled job is failed, like a failed one', () => {
  expect(gateState({ job: { ...preGate, status: 'CANCELLED' }, assignmentVersion: 1 }))
    .toEqual({ kind: 'failed', version: 1 });
});
