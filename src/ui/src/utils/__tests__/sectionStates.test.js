// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/** #2308: "not loaded" and "partial" are told apart from a true "none found". */
import { designBodyState, explorerAvailability } from '../sectionStates';

const synth = (count) => ({ facts: { engines: [{ engine: 'aurora_mysql', target_objects: count }] } });
const none = {};

describe('designBodyState', () => {
  test('tables loaded and counted: ok, one count', () => {
    expect(designBodyState({ hasDesign: true, loadedCount: 20, synthesis: synth(20), engine: 'aurora_mysql'}))
      .toEqual({ state: 'ok', count: 20, loaded: 20, longer: false });
  });
  test('more tables loaded than the report counts: the report figure, never the page own', () => {
    expect(designBodyState({ hasDesign: true, loadedCount: 25, synthesis: synth(20), engine: 'aurora_mysql'}))
      .toEqual({ state: 'ok', count: 20, loaded: 25, longer: true });
  });
  test('a report from before the count fact: ok, the loaded count', () => {
    expect(designBodyState({ hasDesign: true, loadedCount: 3, synthesis: none, engine: 'aurora_mysql'}).count).toBe(3);
  });
  test('fewer tables loaded than the report lists: partial', () => {
    expect(designBodyState({ hasDesign: true, loadedCount: 5, synthesis: synth(20), engine: 'aurora_mysql'}))
      .toEqual({ state: 'partial', count: 20, loaded: 5, longer: false });
  });
  test('the report lists tables, none loaded: unavailable, never a zero', () => {
    expect(designBodyState({ hasDesign: false, loadedCount: 0, synthesis: synth(20), engine: 'aurora_mysql'}).state).toBe('unavailable');
  });
  test('the design files failed to load and the report has no count: unavailable with no figure', () => {
    const r = designBodyState({ hasDesign: false, loadedCount: 0, synthesis: synth(null), engine: 'aurora_mysql', designsLoadFailed: true });
    expect(r).toEqual({ state: 'unavailable', count: null, loaded: 0 });
  });
  test('the design files failed to load: unavailable', () => {
    expect(designBodyState({ hasDesign: false, loadedCount: 0, synthesis: synth(0), engine: 'aurora_mysql', designsLoadFailed: true }).state)
      .toBe('unavailable');
  });
  test('no design and no tables listed: none', () => {
    expect(designBodyState({ hasDesign: false, loadedCount: 0, synthesis: synth(0), engine: 'aurora_mysql'}).state).toBe('none');
    expect(designBodyState({ hasDesign: false, loadedCount: 0, synthesis: synth(null), engine: 'aurora_mysql'}).state).toBe('none');
  });
  test('a design with no tables: empty, which is a real answer', () => {
    expect(designBodyState({ hasDesign: true, loadedCount: 0, synthesis: synth(0), engine: 'aurora_mysql'}).state).toBe('empty');
  });
});

describe('explorerAvailability', () => {
  test('patterns counted, none loaded: unavailable', () => {
    expect(explorerAvailability({ expectedCount: 12, shownCount: 0 })).toBe('unavailable');
  });
  test('nothing counted, nothing loaded: available (a true none)', () => {
    expect(explorerAvailability({ expectedCount: 0, shownCount: 0 })).toBe('available');
  });
  test('fewer rows loaded than counted: partial', () => {
    expect(explorerAvailability({ expectedCount: 12, shownCount: 5 })).toBe('partial');
  });
  test('rows loaded: available', () => {
    expect(explorerAvailability({ expectedCount: 12, shownCount: 12 })).toBe('available');
  });
});

describe('wording of the states (#2308)', () => {
  const en = require('../../locales/en.json');
  test('no design says what is recorded, not a cause', () => {
    const body = en['analysis-results.aurora-design.none-body'];
    expect(body).toContain('has not been made for this assessment yet');
    expect(body).not.toMatch(/without a model|skips/);
  });
  test('the no-count message states no figure', () => {
    expect(en['analysis-results.aurora-design.unavailable-body-nocount']).not.toMatch(/\{\{count\}\}|counts/);
  });
});
