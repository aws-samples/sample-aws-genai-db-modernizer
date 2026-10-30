// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #746: the results page's one line for reality-check moves no model reviewed,
 * from report.json's facts.unreviewed_move_count -- the same line the
 * engineering report and the PDF print. Cloudscape's Alert is a plain DOM
 * stand-in; the component and the English strings are real.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { createInstance } from 'i18next';
import en from '../../locales/en.json';
import UnreviewedMovesNote from '../UnreviewedMovesNote';

jest.mock('@cloudscape-design/components/alert', () => ({
  __esModule: true,
  default: ({ children, type }) => <div data-alert={type}>{children}</div>,
}));

global.IS_REACT_ACT_ENVIRONMENT = true;

let container;
let root;
let t;

beforeAll(async () => {
  const i18n = createInstance();
  await i18n.init({
    resources: { en: { translation: en } },
    lng: 'en',
    fallbackLng: 'en',
    keySeparator: false,
    interpolation: { escapeValue: false },
  });
  t = i18n.t.bind(i18n);
});

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

const render = (synthesis) => act(() => root.render(<UnreviewedMovesNote t={t} synthesis={synthesis} />));

test('states the stored count, in the plural', () => {
  render({ facts: { unreviewed_move_count: 3 } });
  expect(container.textContent).toBe(
    '3 reality-check moves were not reviewed by the model: its review of those moves failed, so they stand as the routing rules placed them.',
  );
});

test('one move reads in the singular', () => {
  render({ facts: { unreviewed_move_count: 1 } });
  expect(container.textContent).toContain('1 reality-check move was not reviewed by the model');
});

test('renders nothing when every move was reviewed, or for an older report', () => {
  render({ facts: { unreviewed_move_count: 0 } });
  expect(container.innerHTML).toBe('');
  render({ facts: {} });
  expect(container.innerHTML).toBe('');
});
