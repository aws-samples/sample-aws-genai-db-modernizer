// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2313: "On this page" in the results page's tools panel: Cloudscape AnchorNavigation
 * over the active tab's sections. Following an anchor goes through the page's own
 * navigation (it opens the section's tab and moves focus there), never the browser's
 * default jump. Cloudscape ships ESM this repo's Jest cannot transform, so
 * AnchorNavigation and HelpPanel are plain-DOM stand-ins that keep their props.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import en from '../../locales/en.json';
import ResultsOnThisPage from '../ResultsOnThisPage';

jest.mock('@cloudscape-design/components/anchor-navigation', () => ({
  __esModule: true,
  default: ({ anchors, onFollow, ariaLabelledby }) => (
    <nav data-labelledby={ariaLabelledby}>
      {anchors.map(a => (
        <a
          key={a.href}
          href={a.href}
          data-level={a.level}
          onClick={(e) => {
            const event = { detail: a, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
            onFollow(event);
            if (event.defaultPrevented) e.preventDefault();
          }}
        >
          {a.text}
        </a>
      ))}
    </nav>
  ),
}));
jest.mock('@cloudscape-design/components/help-panel', () => ({
  __esModule: true,
  default: ({ header, children }) => <aside>{header}{children}</aside>,
}));

global.IS_REACT_ACT_ENVIRONMENT = true;

const t = (key) => en[key] || key;
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

const ANCHORS = [
  { text: 'Migration roadmap', href: '#migration-roadmap-section', level: 1 },
  { text: 'Routines', href: '#routines-section', level: 1 },
];

test('lists the anchors under an "On this page" heading that names the navigation', () => {
  act(() => root.render(<ResultsOnThisPage anchors={ANCHORS} onGo={() => {}} t={t} />));
  const heading = container.querySelector('h2');
  expect(heading.textContent).toBe('On this page');
  expect(container.querySelector('nav').getAttribute('data-labelledby')).toBe(heading.id);
  expect([...container.querySelectorAll('nav a')].map(a => [a.textContent, a.getAttribute('href')])).toEqual([
    ['Migration roadmap', '#migration-roadmap-section'],
    ['Routines', '#routines-section'],
  ]);
});

test('following an anchor goes to its section through the page, not the browser jump', () => {
  const onGo = jest.fn();
  act(() => root.render(<ResultsOnThisPage anchors={ANCHORS} onGo={onGo} t={t} />));
  const link = container.querySelectorAll('nav a')[1];
  const click = new MouseEvent('click', { bubbles: true, cancelable: true });
  act(() => { link.dispatchEvent(click); });
  expect(onGo).toHaveBeenCalledWith('routines-section');
  expect(click.defaultPrevented).toBe(true);
});

test('renders nothing without anchors', () => {
  act(() => root.render(<ResultsOnThisPage anchors={[]} onGo={() => {}} t={t} />));
  expect(container.innerHTML).toBe('');
});
