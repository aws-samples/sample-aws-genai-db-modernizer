// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The explorer workspace's layout, remembered per browser (#308): the side
 * panel's width, whether it is collapsed, its active tab, and whether the
 * experimental notice was dismissed. Layout only; no figure is stored.
 */

export const LAYOUT_KEY = 'dbm-graph-explorer-layout';
export const PANEL_TABS = ['queries', 'walkthrough', 'details'];
export const PANEL_MIN = 300;
export const PANEL_MAX = 720;
export const DEFAULT_LAYOUT = Object.freeze({
  panelWidth: 420,
  collapsed: false,
  tab: 'queries',
  noticeDismissed: false,
});

function browserStorage() {
  try {
    return typeof window !== 'undefined' ? window.localStorage : null;
  } catch (e) {
    return null;
  }
}

export const clampWidth = (width) => Math.min(PANEL_MAX, Math.max(PANEL_MIN, Math.round(Number(width) || DEFAULT_LAYOUT.panelWidth)));

/** The saved layout, with every field checked; the default when none or unreadable. */
export function loadLayout(storage = browserStorage()) {
  try {
    const raw = storage && storage.getItem(LAYOUT_KEY);
    const saved = raw ? JSON.parse(raw) : {};
    return {
      panelWidth: clampWidth(saved.panelWidth ?? DEFAULT_LAYOUT.panelWidth),
      collapsed: saved.collapsed === true,
      tab: PANEL_TABS.includes(saved.tab) ? saved.tab : DEFAULT_LAYOUT.tab,
      noticeDismissed: saved.noticeDismissed === true,
    };
  } catch (e) {
    return { ...DEFAULT_LAYOUT };
  }
}

export function saveLayout(layout, storage = browserStorage()) {
  try {
    if (storage) storage.setItem(LAYOUT_KEY, JSON.stringify(layout));
  } catch (e) {
    // A full or blocked storage only loses the remembered layout.
  }
}

/** Forget the saved layout and return the default. */
export function resetLayout(storage = browserStorage()) {
  try {
    if (storage) storage.removeItem(LAYOUT_KEY);
  } catch (e) {
    // Nothing to forget.
  }
  return { ...DEFAULT_LAYOUT };
}

/** True when a key press should not be taken as a shortcut (the user is typing). */
export function isTyping(target) {
  if (!target) return false;
  const tag = (target.tagName || '').toLowerCase();
  return tag === 'input' || tag === 'textarea' || tag === 'select' || target.isContentEditable === true;
}
