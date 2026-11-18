// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2317: the phases of the progress page and the status of each. The phase before the
 * current one is done, the current one in progress, the rest not started; a finished job
 * has every phase done.
 */
import { HUMAN_STEPS, PIPELINE_STEPS, stepOfStage, stepStatuses } from '../pipelineSteps';

const statuses = (current, complete) => stepStatuses(current, complete).map((s) => s.status);

test('the phases are spelled the American way, in pipeline order', () => {
  expect(PIPELINE_STEPS).toEqual(['collect', 'analyze', 'select', 'design', 'review']);
  expect([...HUMAN_STEPS]).toEqual(['select', 'review']);
});

test('every execution stage maps to the phase it belongs to', () => {
  expect(stepOfStage('RunCollector')).toBe('collect');
  ['RunRefereeTriage', 'LoadTriageOutput', 'RunAnalysis', 'RunAssignmentResolution', 'RunRealityCheck',
    'UpdateAssignmentVersionAfterRealityCheck'].forEach((name) => expect(stepOfStage(name)).toBe('analyze'));
  expect(stepOfStage('WaitForAssignmentApproval')).toBe('select');
  expect(stepOfStage('RunSchemaDesign')).toBe('design');
  expect(stepOfStage('RunRefereeSynthesis')).toBe('review');
  expect(stepOfStage('LoadSynthesisOutput')).toBe('review');
  expect(stepOfStage('SomethingNew')).toBeNull();
  expect(stepOfStage(undefined)).toBeNull();
});

test('a running job has done phases, one in progress and the rest not started', () => {
  expect(statuses('select', false)).toEqual(['success', 'success', 'in-progress', 'pending', 'pending']);
  expect(statuses('collect', false)).toEqual(['in-progress', 'pending', 'pending', 'pending', 'pending']);
});

test('a finished job has every phase done, whatever the current one', () => {
  expect(statuses('review', true)).toEqual(Array(5).fill('success'));
  expect(statuses(null, true)).toEqual(Array(5).fill('success'));
});

test('a job with no known current phase has every phase not started', () => {
  expect(statuses(null, false)).toEqual(Array(5).fill('pending'));
  expect(statuses('SomethingNew', false)).toEqual(Array(5).fill('pending'));
});
