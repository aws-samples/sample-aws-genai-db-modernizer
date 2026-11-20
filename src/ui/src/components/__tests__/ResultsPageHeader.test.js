// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2314: the results page h1 names the assessment ("wordpress assessment"), and its
 * description states the source engine, the assessment id with a copy button and the
 * date the report was written. Real Cloudscape components. A job saved without one of
 * them (no source engine on record, no report timestamp, no database name) leaves that
 * part out and still renders.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import en from '../../locales/en.json';
import ResultsPageHeader, { resultsBreadcrumbs } from '../ResultsPageHeader';

global.IS_REACT_ACT_ENVIRONMENT = true;

let i18n;
let container;
let root;
let warn;
let error;

beforeAll(async () => {
  i18n = createInstance();
  await i18n.init({
    resources: { en: { translation: en } },
    lng: 'en',
    fallbackLng: 'en',
    keySeparator: false,
    interpolation: { escapeValue: false },
  });
});

beforeEach(() => {
  warn = jest.spyOn(console, 'warn').mockImplementation(() => {});
  error = jest.spyOn(console, 'error').mockImplementation(() => {});
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  expect(warn).not.toHaveBeenCalled();
  expect(error).not.toHaveBeenCalled();
  warn.mockRestore();
  error.mockRestore();
});

function render(props) {
  act(() => {
    root.render(
      <I18nextProvider i18n={i18n}>
        <ResultsPageHeader t={i18n.t.bind(i18n)} jobId="8dd35fce" actions={<span>actions</span>} {...props} />
      </I18nextProvider>,
    );
  });
  return createWrapper(container).findHeader();
}

test('the h1 names the database, and the description states source, id and date', () => {
  const header = render({ databaseName: 'wordpress', sourceLabel: 'MySQL', timestamp: '2026-11-18T15:10:47.174567Z' });
  const h1 = container.querySelector('h1');
  expect(h1.textContent).toBe('wordpress assessment');
  const description = header.findDescription().getElement();
  expect(description.textContent).toContain('MySQL source');
  expect(description.textContent).toContain('Assessment id 8dd35fce');
  expect(description.textContent).toContain('Nov 18, 2026');
  // The id has a copy button named for the database it belongs to.
  const copy = createWrapper(description).findCopyToClipboard();
  expect(copy).not.toBeNull();
  expect(copy.findCopyButton().getElement().getAttribute('aria-label')).toContain('8dd35fce');
  // The actions passed in sit in the header.
  expect(header.findActions().getElement().textContent).toBe('actions');
});

test('a hostile database name is text in the title, never markup', () => {
  render({ databaseName: '<img src=x onerror=alert(1)>', sourceLabel: 'MySQL', timestamp: '2026-11-18T00:00:00Z' });
  expect(container.querySelector('img')).toBeNull();
  expect(container.querySelector('h1').textContent).toBe('<img src=x onerror=alert(1)> assessment');
});

test('an old job with no source on record and no report timestamp leaves those parts out', () => {
  const header = render({ databaseName: 'discourse', sourceLabel: null, timestamp: undefined });
  expect(container.querySelector('h1').textContent).toBe('discourse assessment');
  const text = header.findDescription().getElement().textContent;
  expect(text).toContain('Assessment id 8dd35fce');
  expect(text).not.toContain('source');
  expect(text).not.toMatch(/undefined|null|Invalid/);
});

test('with no database name the title still names the assessment', () => {
  render({ databaseName: '', sourceLabel: null, timestamp: null });
  expect(container.querySelector('h1').textContent).toBe('Assessment results');
});

test('the breadcrumb reads Assessments > the database', () => {
  const t = i18n.t.bind(i18n);
  expect(resultsBreadcrumbs(t, 'wordpress', '8dd35fce')).toEqual([
    { href: '/dashboard', text: 'Assessments' },
    { href: '/analysis/results/8dd35fce', text: 'wordpress' },
  ]);
  // No name yet (still loading, or none saved): the id stands in.
  expect(resultsBreadcrumbs(t, '', '8dd35fce')[1].text).toBe('8dd35fce');
});
