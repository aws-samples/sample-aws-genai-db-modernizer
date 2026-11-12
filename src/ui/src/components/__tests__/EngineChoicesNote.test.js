// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2147: the results page's engine-choice lines read the facts' split of tables and
 * statements that read no table, and read exactly as before when there is none.
 * Cloudscape's Alert is a plain DOM stand-in; the component and the English strings
 * are real.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { createInstance } from 'i18next';
import en from '../../locales/en.json';
import EngineChoicesNote from '../EngineChoicesNote';

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

const render = (synthesis) => act(() => root.render(<EngineChoicesNote t={t} synthesis={synthesis} />));

test('the DNN shape: 2 tables and 1 statement with no source table', () => {
  render({
    facts: {
      auto_decided_choice_count: 3, auto_decided_table_count: 2, auto_decided_no_table_count: 1,
      auto_decided_query_count: 3,
      pending_choice_count: 1, pending_choice_table_count: 0, pending_choice_no_table_count: 1,
      pending_choice_query_count: 1,
    },
  });
  expect(container.textContent).toContain(
    'Engine choices for 2 tables and 1 statement with no source table (3 queries) were decided automatically',
  );
  expect(container.textContent).toContain('1 statement with no source table still ties between engines (1 query)');
});

test('with no statement that reads no table it reads as before', () => {
  render({ facts: { auto_decided_choice_count: 3, auto_decided_query_count: 3, pending_choice_count: 2, pending_choice_query_count: 4 } });
  expect(container.textContent).toContain('3 engine choices (3 queries) were decided automatically');
  expect(container.textContent).toContain('2 tables still tie between engines (4 queries)');
  render({
    facts: {
      auto_decided_choice_count: 1, auto_decided_table_count: 1, auto_decided_no_table_count: 0,
      auto_decided_query_count: 2,
    },
  });
  expect(container.textContent).toContain('1 engine choice (2 queries) was decided automatically');
});
