// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { hashId } from './resultsTabs';

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

// Where a linked engine lands: its explorer rows, its Table design tab, 'not-designed'
// (queries are routed to it but its schema design has not been made for this job), or
// 'absent' (the plan has no part for it). 'none' when the link names no engine.
export function linkedEngineTarget(engine, explorerEngines, designEngines, routedEngines = []) {
  if (!engine) return 'none';
  if (explorerEngines.includes(engine)) return 'explorer';
  if (designEngines.includes(engine)) return 'design';
  if (routedEngines.includes(engine)) return 'not-designed';
  return 'absent';
}

// #2313: the section a linked engine lands on: its Table design for a relational
// engine with a design, else the section the link's hash names (the explorer for an
// engine with rows), or null when it names none. The page opens the section's tab.
export const TABLE_DESIGN_SECTION_ID = 'table-design-section';

export function linkedSectionId(target, hash) {
  if (target === 'design') return TABLE_DESIGN_SECTION_ID;
  return hashId(hash);
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

// Results can exist for the job: synthesis has completed (also while a new decision is
// pending, when they are an earlier version's). Before that a request for them fails.
export function synthesisRan(job) {
  const stages = job?.progress?.stages;
  return Array.isArray(stages) && stages.some(
    (st) => (st?.name === 'RunRefereeSynthesis' || st?.name === 'LoadSynthesisOutput') && st?.status === 'completed',
  );
}
