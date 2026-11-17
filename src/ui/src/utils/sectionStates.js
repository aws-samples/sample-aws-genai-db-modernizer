// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2308: a section whose data was not loaded, or only partly, must not read as a true
 * "found nothing". These helpers pick the state each section shows; they count nothing
 * themselves. The figures come from the report's facts (ADR-032) or from the rows the
 * page loaded.
 *
 * Design states:
 *   ok          the loaded tables match the report's count
 *   partial     some tables loaded, fewer than the report lists
 *   unavailable no table loaded, though the report lists some or the design files could not be read
 *   none        no design exists for the engine
 *   empty       a design exists and has no tables
 */
export function designBodyState({ synthesis, engine, hasDesign, loadedCount, designsLoadFailed }) {
  const loaded = Number(loadedCount) || 0;
  // The report's own table count for this engine's design (#1248, facts.engines[].target_objects);
  // null when the report records none.
  const rows = synthesis?.facts?.engines;
  const row = Array.isArray(rows) ? rows.find(r => r && r.engine === engine) : null;
  const reported = typeof row?.target_objects === 'number' ? row.target_objects : null;
  if (loaded > 0) {
    // The count is the report's figure; a report with none shows what loaded.
    const count = reported ?? loaded;
    // longer: the saved design files list more tables than the report counts.
    return {
      state: reported !== null && reported > loaded ? 'partial' : 'ok',
      count,
      loaded,
      longer: reported !== null && loaded > reported,
    };
  }
  if (designsLoadFailed || (reported !== null && reported > 0)) {
    // count is null when the report records none: the page then states no figure.
    return { state: 'unavailable', count: reported, loaded: 0 };
  }
  return { state: hasDesign ? 'empty' : 'none', count: 0, loaded: 0 };
}

/**
 * The explorer lists access patterns from the schema designs. The report counts them
 * once; if it counts some and the page holds none (unavailable) or fewer (partial), the
 * rows could not all be read.
 */
export function explorerAvailability({ expectedCount, shownCount }) {
  const expected = Number(expectedCount) || 0;
  const shown = Number(shownCount) || 0;
  if (expected > 0 && shown === 0) return 'unavailable';
  return expected > shown ? 'partial' : 'available';
}
