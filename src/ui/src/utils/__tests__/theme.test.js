// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2490: the theme follows the system unless you choose Light or Dark.
 *
 * With no saved choice (or System), the page takes the OS `prefers-color-scheme` and
 * follows it when it changes. Light and Dark are saved per browser and win over the OS.
 * jsdom has no `matchMedia`, so each test installs its own stub and pins the system
 * setting it needs; nothing here depends on the machine running the tests.
 */
import {
  THEME_CHOICES,
  THEME_STORAGE_KEY,
  applyThemeChoice,
  installTheme,
  isDarkMode,
  onThemeChange,
  savedThemeChoice,
} from '../theme';

function stubSystem(prefersDark) {
  const listeners = new Set();
  const query = {
    matches: prefersDark,
    media: '(prefers-color-scheme: dark)',
    addEventListener: (type, fn) => type === 'change' && listeners.add(fn),
    removeEventListener: (type, fn) => type === 'change' && listeners.delete(fn),
  };
  window.matchMedia = jest.fn(() => query);
  return {
    flip(dark) {
      query.matches = dark;
      listeners.forEach((fn) => fn({ matches: dark }));
    },
    listenerCount: () => listeners.size,
  };
}

let uninstall = null;

afterEach(() => {
  if (uninstall) uninstall();
  uninstall = null;
  delete window.matchMedia;
  localStorage.clear();
  document.body.classList.remove('awsui-dark-mode');
});

test('the choices are System, Light and Dark, in that order', () => {
  expect(THEME_CHOICES).toEqual(['system', 'light', 'dark']);
});

test('with no saved choice the page follows a dark system', () => {
  stubSystem(true);
  uninstall = installTheme();
  expect(savedThemeChoice()).toBe('system');
  expect(isDarkMode()).toBe(true);
});

test('with no saved choice the page follows a light system, never dark by default', () => {
  stubSystem(false);
  uninstall = installTheme();
  expect(isDarkMode()).toBe(false);
});

test('a browser with no matchMedia reads as light', () => {
  delete window.matchMedia;
  uninstall = installTheme();
  expect(isDarkMode()).toBe(false);
});

test('on System the page follows the OS when it changes, and says so', () => {
  const system = stubSystem(false);
  uninstall = installTheme();
  const seen = [];
  const off = onThemeChange(() => seen.push(isDarkMode()));
  system.flip(true);
  expect(isDarkMode()).toBe(true);
  system.flip(false);
  expect(isDarkMode()).toBe(false);
  off();
  expect(seen).toEqual([true, false]);
});

test('a saved Light or Dark choice wins over the OS and its changes', () => {
  const system = stubSystem(true);
  localStorage.setItem(THEME_STORAGE_KEY, 'light');
  uninstall = installTheme();
  expect(isDarkMode()).toBe(false);
  system.flip(false);
  system.flip(true);
  expect(isDarkMode()).toBe(false);

  applyThemeChoice('dark');
  system.flip(false);
  expect(isDarkMode()).toBe(true);
});

test('choosing a theme saves it, and choosing System clears the saved choice', () => {
  const system = stubSystem(false);
  uninstall = installTheme();
  applyThemeChoice('dark');
  expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe('dark');
  expect(isDarkMode()).toBe(true);
  applyThemeChoice('light');
  expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe('light');
  expect(isDarkMode()).toBe(false);

  applyThemeChoice('system');
  expect(localStorage.getItem(THEME_STORAGE_KEY)).toBeNull();
  expect(savedThemeChoice()).toBe('system');
  system.flip(true);
  expect(isDarkMode()).toBe(true);
});

test('each choice tells the page the theme changed', () => {
  stubSystem(false);
  uninstall = installTheme();
  const seen = [];
  const off = onThemeChange(() => seen.push(isDarkMode()));
  applyThemeChoice('dark');
  applyThemeChoice('system');
  off();
  applyThemeChoice('dark');
  expect(seen).toEqual([true, false]);
});

test('a saved value from an older build or an unknown one reads as its choice or System', () => {
  localStorage.setItem(THEME_STORAGE_KEY, 'dark');
  expect(savedThemeChoice()).toBe('dark');
  localStorage.setItem(THEME_STORAGE_KEY, 'light');
  expect(savedThemeChoice()).toBe('light');
  localStorage.setItem(THEME_STORAGE_KEY, 'purple');
  expect(savedThemeChoice()).toBe('system');
});

test('uninstalling stops following the OS', () => {
  const system = stubSystem(false);
  const off = installTheme();
  expect(system.listenerCount()).toBe(1);
  off();
  expect(system.listenerCount()).toBe(0);
  system.flip(true);
  expect(isDarkMode()).toBe(false);
});


// The browser chrome colour follows the mode, and it is the page background's token value.
describe('the theme-color meta tag', () => {
  let meta;
  beforeEach(() => {
    meta = document.createElement('meta');
    meta.setAttribute('name', 'theme-color');
    meta.setAttribute('content', '#ffffff');
    document.head.appendChild(meta);
  });
  afterEach(() => meta.remove());

  test('takes the page background of the mode the page applies', () => {
    const { PAGE_THEME_COLOR } = require('../../styles/palette');
    const system = stubSystem(false);
    uninstall = installTheme();
    expect(meta.getAttribute('content')).toBe(PAGE_THEME_COLOR.light);
    system.flip(true);
    expect(meta.getAttribute('content')).toBe(PAGE_THEME_COLOR.dark);
    applyThemeChoice('light');
    expect(meta.getAttribute('content')).toBe(PAGE_THEME_COLOR.light);
  });

  test('its two colours are colorBackgroundLayoutMain in each theme', () => {
    const { PAGE_THEME_COLOR } = require('../../styles/palette');
    const { colorBackgroundLayoutMain } = require('@cloudscape-design/design-tokens');
    const { tokenValues } = require('../../testUtils/themeTokenValues');
    const values = tokenValues(colorBackgroundLayoutMain);
    expect(values.defined).toBe(true);
    expect(PAGE_THEME_COLOR).toEqual({ light: values.light, dark: values.dark });
  });
});
