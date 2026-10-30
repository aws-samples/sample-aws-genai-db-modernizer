// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #1003: the assignment review's per-table engine choices, and the results
 * page's line for them. Cloudscape components are plain DOM stand-ins; the
 * components and the English strings are real.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { createInstance } from 'i18next';
import en from '../../locales/en.json';
import PendingEngineChoices from '../PendingEngineChoices';
import EngineChoicesNote from '../EngineChoicesNote';

const passthrough = (tag) => ({ __esModule: true, default: ({ children, header }) => (
  <div data-tag={tag}>{header}{children}</div>
) });
jest.mock('@cloudscape-design/components/container', () => passthrough('container'));
jest.mock('@cloudscape-design/components/space-between', () => passthrough('space'));
jest.mock('@cloudscape-design/components/box', () => passthrough('box'));
jest.mock('@cloudscape-design/components/badge', () => passthrough('badge'));
jest.mock('@cloudscape-design/components/alert', () => passthrough('alert'));
jest.mock('@cloudscape-design/components/expandable-section', () => ({
  __esModule: true,
  default: ({ children, headerText }) => <div data-tag="expand">{headerText}{children}</div>,
}));
jest.mock('@cloudscape-design/components/header', () => ({
  __esModule: true,
  default: ({ children, actions, description }) => (
    <div data-tag="header"><h2>{children}</h2><p>{description}</p>{actions}</div>
  ),
}));
jest.mock('@cloudscape-design/components/button', () => ({
  __esModule: true,
  default: ({ children, onClick, disabled }) => (
    <button type="button" onClick={onClick} disabled={disabled}>{children}</button>
  ),
}));
jest.mock('@cloudscape-design/components/select', () => ({
  __esModule: true,
  default: ({ options, onChange, selectedOption, placeholder, ariaLabel }) => (
    <select
      aria-label={ariaLabel}
      value={selectedOption ? selectedOption.value : ''}
      onChange={(e) => onChange({ detail: { selectedOption: options.find((o) => o.value === e.target.value) } })}
    >
      <option value="">{placeholder}</option>
      {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
    </select>
  ),
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

const CHOICE = {
  choice_id: 'discourse.theme_fields',
  table: 'discourse.theme_fields',
  query_ids: ['q1', 'q2'],
  tied_engines: ['documentdb', 'dynamodb'],
  current_engine: 'dynamodb',
  options: [
    { engine: 'documentdb', score: 75, unrounded_score: 75.3, trade_off: 'DocumentDB trade-off.' },
    { engine: 'dynamodb', score: 75, unrounded_score: 75.55, trade_off: 'DynamoDB trade-off.' },
  ],
  why_tied: 'Both score 75.',
  decide_for_me_engine: 'dynamodb',
  decide_for_me_rule: 'unrounded_score',
  decide_for_me_reason: 'DynamoDB scores higher before rounding.',
};
const DECIDED = {
  choice_id: 'discourse.incoming_emails',
  table: 'discourse.incoming_emails',
  query_ids: ['q3'],
  engine: 'documentdb',
  alternatives: ['dynamodb'],
  reason: 'DocumentDB scores higher before rounding.',
};

test('shows one choice per table with its scores, trade-offs and queries, and sends the pick', () => {
  const onSubmit = jest.fn();
  act(() => root.render(<PendingEngineChoices t={t} choices={[CHOICE]} decided={[]} onSubmit={onSubmit} />));
  const text = container.textContent;
  expect(text).toContain('1 pending engine choice');
  expect(text).toContain('theme_fields (2 queries)');
  expect(text).toContain('75 (75.55 before rounding)');
  expect(text).toContain('DocumentDB trade-off.');
  expect(text).toContain('Decide for me picks dynamodb: DynamoDB scores higher before rounding.');
  expect(text).toContain('q1, q2');
  // The picker has a real accessible name, from the component itself.
  const select = container.querySelector('select');
  expect(select.getAttribute('aria-label')).toBe('Engine for theme_fields');
  act(() => {
    select.value = 'documentdb';
    select.dispatchEvent(new Event('change', { bubbles: true }));
  });
  const save = [...container.querySelectorAll('button')].find((b) => b.textContent.startsWith('Save'));
  act(() => save.click());
  expect(onSubmit).toHaveBeenCalledWith({
    table_choices: [{ choice_id: 'discourse.theme_fields', engine: 'documentdb' }],
    overrides: [],
  });
});

test('decide for me sends the flag, never a pick of its own', () => {
  const onSubmit = jest.fn();
  act(() => root.render(<PendingEngineChoices t={t} choices={[CHOICE]} decided={[]} onSubmit={onSubmit} />));
  const button = [...container.querySelectorAll('button')].find((b) => b.textContent === 'Decide for me');
  act(() => button.click());
  expect(onSubmit).toHaveBeenCalledWith({ table_choices: [], decide_for_me: true, overrides: [] });
});

test('marks a decided choice and switches it to its alternative', () => {
  const onSubmit = jest.fn();
  act(() => root.render(<PendingEngineChoices t={t} choices={[]} decided={[DECIDED]} onSubmit={onSubmit} />));
  expect(container.textContent).toContain('Decided automatically, review if needed');
  const select = container.querySelector('select');
  expect(select.getAttribute('aria-label')).toBe('Engine for incoming_emails');
  act(() => {
    select.value = 'dynamodb';
    select.dispatchEvent(new Event('change', { bubbles: true }));
  });
  const button = [...container.querySelectorAll('button')].find((b) => b.textContent.startsWith('Switch'));
  act(() => button.click());
  expect(onSubmit).toHaveBeenCalledWith({
    table_choices: [],
    overrides: [{ query_id: 'q3', assigned_engine: 'dynamodb' }],
  });
});

test('renders nothing without choices', () => {
  act(() => root.render(<PendingEngineChoices t={t} choices={[]} decided={[]} onSubmit={() => {}} />));
  expect(container.textContent).toBe('');
});

test('the results page states the stored counts', () => {
  act(() => root.render(<EngineChoicesNote t={t} synthesis={{ facts: {
    pending_choice_count: 9, pending_choice_query_count: 32,
    auto_decided_choice_count: 1, auto_decided_query_count: 3,
  } }} />));
  expect(container.textContent).toContain('9 tables still tie between engines (32 queries)');
  expect(container.textContent).toContain('1 engine choice (3 queries) was decided automatically: review it if needed');
});
