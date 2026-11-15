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
