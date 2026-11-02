// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #802: the results page lists each failure the DynamoDB group merge left unresolved,
 * with its access patterns and queries and what they are (unserved, at risk, none
 * named, not recorded), from report.json's facts.dynamodb_merge_conflicts. The fixture
 * is the facts layer's output (tests/unit/report/test_merge_conflict_sections.py checks
 * it), and its not_served_lines are the lines the engineering report prints too.
 * Cloudscape's components are plain DOM stand-ins; the component and the English
 * strings are real.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { createInstance } from 'i18next';
import en from '../../locales/en.json';
import fixture from './fixtures/dynamodbMergeConflicts.json';
import DynamoDBMergeConflictsCallout from '../DynamoDBMergeConflictsCallout';

jest.mock('@cloudscape-design/components/alert', () => ({
  __esModule: true,
  default: ({ children, header }) => <div data-alert="warning"><h3>{header}</h3>{children}</div>,
}));
jest.mock('@cloudscape-design/components/box', () => ({
  __esModule: true,
  default: ({ children }) => <div>{children}</div>,
}));
jest.mock('@cloudscape-design/components/space-between', () => ({
  __esModule: true,
  default: ({ children }) => <div>{children}</div>,
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

const render = (synthesis) => act(() => root.render(<DynamoDBMergeConflictsCallout t={t} synthesis={synthesis} />));
const texts = (figure) => [...container.querySelectorAll(`[data-figure="${figure}"]`)].map(n => n.textContent);
const copy = (value) => JSON.parse(JSON.stringify(value));

test('lists each failure with what it leaves unserved, at risk or names none of', () => {
  render({ facts: { dynamodb_merge_conflicts: fixture.facts } });
  expect(container.querySelector('h3').textContent).toBe('Access patterns the merged DynamoDB design does not serve');
  expect(texts('merge-conflict-summary')).toEqual([fixture.facts.summary_line]);
  expect(texts('merge-failure')).toEqual(fixture.facts.conflicts.map(c => c.failure));
  expect(texts('merge-unserved')).toEqual(fixture.not_served_lines);
  expect(container.textContent).not.toContain('No access pattern is left unserved');
});

test('a design merged before the patterns were recorded says they are unknown', () => {
  render({ facts: { dynamodb_merge_conflicts: fixture.legacy.facts } });
  expect(texts('merge-conflict-summary')).toEqual([fixture.legacy.facts.summary_line]);
  expect(texts('merge-unserved')).toEqual(fixture.legacy.not_served_lines);
});

test('prints the stored facts, not a count of its own', () => {
  const facts = copy(fixture.facts);
  facts.summary_line = 'Stored line.';
  facts.conflicts[0].pattern_ids = ['DDB-AP-9'];
  render({ facts: { dynamodb_merge_conflicts: facts } });
  expect(texts('merge-conflict-summary')).toEqual(['Stored line.']);
  expect(texts('merge-unserved')[0]).toBe('Not served: DDB-AP-9 on Main; queries q-users-by-id, q-users-by-login.');
});

test('two identical failures are two rows, each with its own line', () => {
  const facts = copy(fixture.facts);
  const failure = 'DynamoDB merge: access pattern runs on table \'Gone\', but no table of that name is defined';
  facts.conflicts = [
    { failure, status: 'unserved', table_name: 'Gone', pattern_ids: ['DDB-AP-1'], query_ids: ['q-1'], repeated_pattern_ids: [] },
    { failure, status: 'unserved', table_name: 'Gone', pattern_ids: ['DDB-AP-2'], query_ids: ['q-2'], repeated_pattern_ids: [] },
  ];
  const errors = jest.spyOn(console, 'error').mockImplementation(() => {});
  render({ facts: { dynamodb_merge_conflicts: facts } });
  expect(texts('merge-failure')).toEqual([failure, failure]);
  expect(texts('merge-unserved')).toEqual([
    'Not served: DDB-AP-1 on Gone; queries q-1.',
    'Not served: DDB-AP-2 on Gone; queries q-2.',
  ]);
  expect(errors.mock.calls.filter(c => String(c[0]).includes('same key'))).toEqual([]);
  errors.mockRestore();
});

test('customer text is printed as text, never as markup', () => {
  const facts = copy(fixture.facts);
  facts.conflicts[0].failure = 'DynamoDB merge: <b>bold</b>';
  render({ facts: { dynamodb_merge_conflicts: facts } });
  expect(container.querySelector('b')).toBeNull();
  expect(texts('merge-failure')[0]).toBe('DynamoDB merge: <b>bold</b>');
});

test('renders nothing for a clean merge, or for a report written before the fact', () => {
  render({ facts: { dynamodb_merge_conflicts: null } });
  expect(container.innerHTML).toBe('');
  render({ facts: {} });
  expect(container.innerHTML).toBe('');
  render({ facts: { dynamodb_merge_conflicts: { conflicts: [] } } });
  expect(container.innerHTML).toBe('');
});
