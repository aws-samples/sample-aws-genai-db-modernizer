// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #1780: the results page lists the DynamoDB merge-review decisions (each source table's
 * authoritative home and derived copies) from report.json's facts.dynamodb_merge_reviews.
 * The fixture is the facts layer's output (tests/unit/report/test_merge_review_decisions.py
 * checks it, and that the engineering report prints the same lines and the executive
 * report the same sentences). Cloudscape's components are plain DOM stand-ins; the
 * component and the English strings are real.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { createInstance } from 'i18next';
import en from '../../locales/en.json';
import fixture from './fixtures/dynamodbMergeReviews.json';
import DynamoDBMergeReviewsCallout from '../DynamoDBMergeReviewsCallout';

jest.mock('@cloudscape-design/components/alert', () => ({
  __esModule: true,
  default: ({ children, header }) => <div data-alert="info"><h3>{header}</h3>{children}</div>,
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

const render = (synthesis) => act(() => root.render(<DynamoDBMergeReviewsCallout t={t} synthesis={synthesis} />));
const texts = (figure) => [...container.querySelectorAll(`[data-figure="${figure}"]`)].map(n => n.textContent);
const copy = (value) => JSON.parse(JSON.stringify(value));

test('lists each decision under the sentences both reports state', () => {
  render({ facts: { dynamodb_merge_reviews: fixture.facts } });
  expect(container.querySelector('h3').textContent).toBe(fixture.title);
  expect(texts('merge-review-summary')).toEqual([fixture.facts.summary_line]);
  expect(texts('merge-review-review')).toEqual([fixture.facts.review_line]);
  expect(texts('merge-review-decision')).toEqual(fixture.facts.decisions.map(d => d.line));
  expect(texts('merge-review-decision')[0]).toBe(
    'wp_posts: the merge chose WpPosts as the authoritative home, which takes every write to it; '
    + 'wp_posts is a derived copy updated from the DynamoDB Streams of WpPosts (RISK-004).',
  );
});

test('prints the stored facts, not a line of its own', () => {
  const facts = copy(fixture.facts);
  facts.summary_line = 'Stored summary.';
  facts.decisions[1].line = 'Stored line.';
  render({ facts: { dynamodb_merge_reviews: facts } });
  expect(texts('merge-review-summary')).toEqual(['Stored summary.']);
  expect(texts('merge-review-decision')[1]).toBe('Stored line.');
});

test('customer text is printed as text, never as markup', () => {
  const facts = copy(fixture.facts);
  facts.decisions[0].line = '<b>Home</b>: the merge chose it';
  render({ facts: { dynamodb_merge_reviews: facts } });
  expect(container.querySelector('b')).toBeNull();
  expect(texts('merge-review-decision')[0]).toBe('<b>Home</b>: the merge chose it');
});

test('renders nothing with no decision, or for a report written before the fact', () => {
  render({ facts: { dynamodb_merge_reviews: null } });
  expect(container.innerHTML).toBe('');
  render({ facts: {} });
  expect(container.innerHTML).toBe('');
  render({ facts: { dynamodb_merge_reviews: { decisions: [] } } });
  expect(container.innerHTML).toBe('');
});
