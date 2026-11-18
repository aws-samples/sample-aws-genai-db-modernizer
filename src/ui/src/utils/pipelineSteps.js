// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// The five phases the progress page shows (#2317), and the status of each while a
// job runs. The labels live in the locale file under `job-summary.steps.<id>`.

export const PIPELINE_STEPS = ['collect', 'analyze', 'select', 'design', 'review'];

// The phases that wait for a person (the assignment choice and the final review).
export const HUMAN_STEPS = new Set(['select', 'review']);

// The execution's stage names, by the phase each belongs to.
const STAGE_STEP = {
  RunCollector: 'collect',
  RunRefereeTriage: 'analyze',
  LoadTriageOutput: 'analyze',
  RunAnalysis: 'analyze',
  RunAssignmentResolution: 'analyze',
  RunRealityCheck: 'analyze',
  UpdateAssignmentVersionAfterRealityCheck: 'analyze',
  WaitForAssignmentApproval: 'select',
  RunSchemaDesign: 'design',
  RunRefereeSynthesis: 'review',
  LoadSynthesisOutput: 'review',
};

/** The phase a stage name belongs to, or null for a name no phase knows. */
export const stepOfStage = (stageName) => STAGE_STEP[stageName] || null;

/**
 * Each phase with its Cloudscape status: every phase before the current one is
 * `success`, the current one `in-progress`, the rest `pending`. A finished job has
 * every phase `success`; with no current phase, every phase is `pending`.
 */
export function stepStatuses(current, complete) {
  const at = PIPELINE_STEPS.indexOf(current);
  return PIPELINE_STEPS.map((id, index) => {
    if (complete || (at >= 0 && index < at)) return { id, status: 'success' };
    if (index === at) return { id, status: 'in-progress' };
    return { id, status: 'pending' };
  });
}
