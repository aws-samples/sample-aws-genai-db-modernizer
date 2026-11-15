// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// #2300: where an engine lives on the results page. The access pattern explorer
// (its container's id) has an engine filter, so a link carries the engine in
// `?engine=` and the section in the hash; the results page reads both.
export const RESULTS_ENGINE_SECTION_ID = 'access-patterns-section';

export function resultsEngineHref(jobId, engine) {
  const query = engine ? `?engine=${encodeURIComponent(engine)}` : '';
  return `/analysis/results/${encodeURIComponent(jobId)}${query}#${RESULTS_ENGINE_SECTION_ID}`;
}

// The engine a results link names, or null when it names none.
export function engineFromSearch(search) {
  return new URLSearchParams(search || '').get('engine') || null;
}

// Where a linked engine lands: its explorer rows, its Table design tab, or nowhere
// ('absent': the plan has no part for it, and the page says so). 'none' when the link
// names no engine.
export function linkedEngineTarget(engine, explorerEngines, designEngines) {
  if (!engine) return 'none';
  if (explorerEngines.includes(engine)) return 'explorer';
  if (designEngines.includes(engine)) return 'design';
  return 'absent';
}

// The Table design tab shown: the linked engine's when it has a design, else the
// first. Always defined while there is a design, so the tabs are controlled from
// their first render (Cloudscape decides that once, on the first render).
export function activeDesignEngine(selected, designEngines) {
  return designEngines.includes(selected) ? selected : designEngines[0];
}

// A decision is pending at the assignment gate: the approval stage has not completed.
export function awaitsApproval(job) {
  const stages = job?.progress?.stages;
  const last = Array.isArray(stages) ? stages[stages.length - 1] : null;
  return last?.name === 'WaitForAssignmentApproval' && last?.status !== 'completed';
}
