// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { applyMode, Mode } from '@cloudscape-design/global-styles';
import { PAGE_THEME_COLOR } from '../styles/palette';

/**
 * The page theme (#2490). The Theme menu offers System, Light and Dark. System is the
 * default: the page takes the OS `prefers-color-scheme` and follows it when it changes.
 * Light and Dark are saved in this browser's localStorage and win over the OS; System
 * saves nothing. This file is the only one that reads or writes the saved choice and
 * the only one that calls applyMode (utils/__tests__/themeOwnership.test.js); a component that draws per theme asks `isDarkMode()`
 * for the mode the page applied and listens with `onThemeChange`.
 */
export const THEME_STORAGE_KEY = 'dbm-theme';
export const THEME_CHOICES = ['system', 'light', 'dark'];
export const THEME_CHANGE_EVENT = 'dbm-theme-change';

const DARK_QUERY = '(prefers-color-scheme: dark)';
const DARK_CLASS = 'awsui-dark-mode';

function systemQuery() {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function'
    ? window.matchMedia(DARK_QUERY)
    : null;
}

/** The saved choice: 'light', 'dark', or 'system' when none (or an unknown one) is saved. */
export function savedThemeChoice() {
  let saved = null;
  try {
    saved = window.localStorage.getItem(THEME_STORAGE_KEY);
  } catch {
    saved = null;
  }
  return saved === 'light' || saved === 'dark' ? saved : 'system';
}

/** True when the page shows dark mode now, whatever chose it. */
export function isDarkMode() {
  return document.body.classList.contains(DARK_CLASS);
}

function applyChoice(choice) {
  const query = systemQuery();
  const dark = choice === 'dark' || (choice === 'system' && Boolean(query && query.matches));
  applyMode(dark ? Mode.Dark : Mode.Light);
  // The browser's chrome (a mobile address bar, a PWA title bar) matches the page.
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', dark ? PAGE_THEME_COLOR.dark : PAGE_THEME_COLOR.light);
  window.dispatchEvent(new Event(THEME_CHANGE_EVENT));
}

/** Save `choice` ('system' clears the saved one) and apply it. */
export function applyThemeChoice(choice) {
  const known = THEME_CHOICES.includes(choice) ? choice : 'system';
  try {
    if (known === 'system') window.localStorage.removeItem(THEME_STORAGE_KEY);
    else window.localStorage.setItem(THEME_STORAGE_KEY, known);
  } catch {
    // No storage (a locked-down browser): the choice holds for this page only.
  }
  applyChoice(known);
}

/** Apply the saved choice and follow the OS while it is System. Returns the uninstall. */
export function installTheme() {
  applyChoice(savedThemeChoice());
  const query = systemQuery();
  if (!query || typeof query.addEventListener !== 'function') return () => {};
  const follow = () => {
    if (savedThemeChoice() === 'system') applyChoice('system');
  };
  query.addEventListener('change', follow);
  return () => query.removeEventListener('change', follow);
}

/** Call `listener` after every theme change. Returns the unsubscribe. */
export function onThemeChange(listener) {
  window.addEventListener(THEME_CHANGE_EVENT, listener);
  return () => window.removeEventListener(THEME_CHANGE_EVENT, listener);
}
