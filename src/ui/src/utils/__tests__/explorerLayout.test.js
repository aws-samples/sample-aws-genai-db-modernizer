// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { DEFAULT_LAYOUT, LAYOUT_KEY, PANEL_MAX, PANEL_MIN, TABS_MIN_WIDTH, clampWidth, isTyping, loadLayout, resetLayout, saveLayout } from '../explorerLayout';

function memoryStorage(initial = {}) {
  const data = { ...initial };
  return {
    getItem: (k) => (k in data ? data[k] : null),
    setItem: (k, v) => {
      data[k] = String(v);
    },
    removeItem: (k) => {
      delete data[k];
    },
    data,
  };
}

test('the layout round-trips through storage', () => {
  const storage = memoryStorage();
  saveLayout({ panelWidth: 500, collapsed: true, tab: 'details', noticeDismissed: true }, storage);
  expect(loadLayout(storage)).toEqual({ panelWidth: 500, collapsed: true, tab: 'details', noticeDismissed: true });
});

test('a missing, broken or odd saved layout falls back field by field', () => {
  expect(loadLayout(memoryStorage())).toEqual(DEFAULT_LAYOUT);
  expect(loadLayout(memoryStorage({ [LAYOUT_KEY]: '{not json' }))).toEqual(DEFAULT_LAYOUT);
  const odd = loadLayout(memoryStorage({ [LAYOUT_KEY]: JSON.stringify({ panelWidth: 99999, collapsed: 'yes', tab: 'nope' }) }));
  expect(odd).toEqual({ ...DEFAULT_LAYOUT, panelWidth: PANEL_MAX });
  expect(loadLayout(null)).toEqual(DEFAULT_LAYOUT);
});

test('the panel width stays between its bounds', () => {
  expect(clampWidth(10)).toBe(PANEL_MIN);
  expect(clampWidth(10000)).toBe(PANEL_MAX);
  expect(clampWidth('abc')).toBe(DEFAULT_LAYOUT.panelWidth);
});

test('reset forgets the saved layout', () => {
  const storage = memoryStorage({ [LAYOUT_KEY]: JSON.stringify({ collapsed: true }) });
  expect(resetLayout(storage)).toEqual(DEFAULT_LAYOUT);
  expect(storage.data[LAYOUT_KEY]).toBeUndefined();
});

test('a key typed into a field is not a shortcut', () => {
  expect(isTyping({ tagName: 'INPUT' })).toBe(true);
  expect(isTyping({ tagName: 'DIV', isContentEditable: true })).toBe(true);
  expect(isTyping({ tagName: 'DIV' })).toBe(false);
  expect(isTyping(null)).toBe(false);
});

// #308 Questions tab: four tabs fit the default panel without scroll arrows.
test('the default panel is wide enough for the four tabs', () => {
  expect(DEFAULT_LAYOUT.panelWidth).toBe(TABS_MIN_WIDTH);
  expect(TABS_MIN_WIDTH).toBe(480);
  expect(TABS_MIN_WIDTH).toBeLessThanOrEqual(PANEL_MAX);
});

test('a saved width too narrow for the four tabs is raised on load; a wider one is kept', () => {
  const at = (panelWidth) => loadLayout(memoryStorage({ [LAYOUT_KEY]: JSON.stringify({ panelWidth }) })).panelWidth;
  expect(at(360)).toBe(TABS_MIN_WIDTH);
  expect(at(420)).toBe(TABS_MIN_WIDTH);
  expect(at(600)).toBe(600);
  expect(at(PANEL_MAX + 100)).toBe(PANEL_MAX);
});
