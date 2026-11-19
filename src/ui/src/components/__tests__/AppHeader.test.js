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
