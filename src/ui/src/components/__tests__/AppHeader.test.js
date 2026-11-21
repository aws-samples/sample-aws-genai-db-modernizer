// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2314: the top navigation has a Help link to the help site, a real Cloudscape
 * TopNavigation utility with a text label that opens in a new tab. A page can point
 * it at the help page that explains it.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import '../../i18n';
import AppHeader from '../AppHeader';
import { HELP_SITE_URL, RESULTS_HELP_URL } from '../../utils/helpSite';

global.IS_REACT_ACT_ENVIRONMENT = true;

let container;
let root;

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

function helpLink(props) {
  act(() => {
    root.render(<AppHeader {...props} />);
  });
  // TopNavigation also renders hidden copies to measure its utilities; read the shown one.
  const nav = createWrapper(container).findTopNavigation();
  const link = nav.findUtility(1).findButtonLinkType().getElement();
  expect(link.tagName).toBe('A');
  return link;
}

test('the Help utility links to the help site and opens it in a new tab', () => {
  const link = helpLink({});
  expect(link.getAttribute('href')).toBe(HELP_SITE_URL);
  expect(link.getAttribute('target')).toBe('_blank');
  expect(link.getAttribute('rel')).toContain('noopener');
  // A text label, not an icon only, and its name says it opens a new tab.
  expect(link.textContent.trim()).toBe('Help');
  expect(link.getAttribute('aria-label')).toBe('Help Opens in a new tab');
});

test('a page can point Help at its own help page', () => {
  expect(helpLink({ helpHref: RESULTS_HELP_URL }).getAttribute('href')).toBe(RESULTS_HELP_URL);
});

test('the Help utility is one of the TopNavigation utilities', () => {
  act(() => {
    root.render(<AppHeader />);
  });
  const nav = createWrapper(container).findTopNavigation();
  const texts = nav.findUtilities().map(u => u.getElement().textContent);
  expect(texts.some(text => text.includes('Help'))).toBe(true);
});

// #2490: a Theme menu with a text label (System, Light, Dark) replaces the star icon.
// System is the default; Light and Dark are saved in this browser.
describe('the Theme menu', () => {
  // The header is still mounted here, and Cloudscape re-reads the mode when <body>'s
  // class changes, so the reset runs inside act.
  afterEach(async () => {
    localStorage.clear();
    await act(async () => document.body.classList.remove('awsui-dark-mode'));
  });

  function themeMenu() {
    act(() => {
      root.render(<AppHeader />);
    });
    const menu = createWrapper(container).findTopNavigation().findUtility(2).findMenuDropdownType();
    expect(menu).not.toBeNull();
    return menu;
  }

  // The dropdown and Cloudscape's mode observer update after the event, so each step
  // waits for them inside an async act.
  async function open(menu) {
    await act(async () => menu.openDropdown());
  }

  async function choose(menu, id) {
    await open(menu);
    await act(async () => menu.findItemById(id).click());
  }

  test('is a text menu named Theme with System, Light and Dark, and no star icon', async () => {
    const menu = themeMenu();
    expect(menu.findNativeButton().getElement().textContent.trim()).toBe('Theme');
    // The old icon-only toggle ("Switch to light theme") is gone.
    expect(container.querySelector('[aria-label*="Switch to"], [title*="Switch to"]')).toBeNull();
    await open(menu);
    expect(menu.findItemById('system').getElement().textContent).toBe('System');
    expect(menu.findItemById('light').getElement().textContent).toBe('Light');
    expect(menu.findItemById('dark').getElement().textContent).toBe('Dark');
  });

  test('checks System when nothing is saved', async () => {
    const menu = themeMenu();
    await open(menu);
    expect(menu.findItemCheckedById('system')).toBe('true');
    expect(menu.findItemCheckedById('light')).toBe('false');
    expect(menu.findItemCheckedById('dark')).toBe('false');
  });

  test('choosing Dark saves it, applies it and checks it', async () => {
    const menu = themeMenu();
    await choose(menu, 'dark');
    expect(localStorage.getItem('dbm-theme')).toBe('dark');
    expect(document.body.classList.contains('awsui-dark-mode')).toBe(true);
    await open(menu);
    expect(menu.findItemCheckedById('dark')).toBe('true');
    expect(menu.findItemCheckedById('system')).toBe('false');
  });

  test('choosing Light, then System, clears the saved choice', async () => {
    const menu = themeMenu();
    await choose(menu, 'light');
    expect(localStorage.getItem('dbm-theme')).toBe('light');
    expect(document.body.classList.contains('awsui-dark-mode')).toBe(false);
    await choose(menu, 'system');
    expect(localStorage.getItem('dbm-theme')).toBeNull();
    await open(menu);
    expect(menu.findItemCheckedById('system')).toBe('true');
  });

  test('opens on the saved choice', async () => {
    localStorage.setItem('dbm-theme', 'light');
    const menu = themeMenu();
    await open(menu);
    expect(menu.findItemCheckedById('light')).toBe('true');
  });
});
