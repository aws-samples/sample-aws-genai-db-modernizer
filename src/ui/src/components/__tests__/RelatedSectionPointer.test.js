// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2313 review: the queries that cannot be served as designed (Plan) and the access
 * patterns the merged DynamoDB design does not serve, with its merge-review decisions
 * (Risks and decisions), are on two tabs. Each side points at the other, when the other
 * is there, in one line whose link goes through the page's navigation.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import en from '../../locales/en.json';
import RelatedSectionPointer, { unservedPointers } from '../RelatedSectionPointer';
import { ResultsSectionNav } from '../../hooks/useResultsTabs';

jest.mock('@cloudscape-design/components/box', () => ({
  __esModule: true,
  default: ({ children }) => <div className="mock-box">{children}</div>,
}));
jest.mock('@cloudscape-design/components/link', () => ({
  __esModule: true,
  default: ({ children, href, onFollow }) => <a href={href} onClick={onFollow}>{children}</a>,
}));

global.IS_REACT_ACT_ENVIRONMENT = true;

const t = (key) => en[key] || key;
const UNSERVABLE = { unservable_query_count: 1, unservable_queries: [{ query_id: 'q1' }] };
const CONFLICTS = { conflict_count: 1, conflicts: [{ kind: 'unserved', access_patterns: ['AP-1'] }] };
const REVIEWS = { decisions: [{ line: 'x' }], summary_line: 's', review_line: 'r' };
const facts = (f) => ({ facts: f });

describe('which pointer each side shows', () => {
  test('both sides: the banner points at the merge conflicts, they point back', () => {
    expect(unservedPointers(facts({ unservable: UNSERVABLE, dynamodb_merge_conflicts: CONFLICTS })))
      .toEqual({ toRisks: 'dynamodb-merge-conflicts', toPlan: 'unservable-section' });
  });

  test('only merge reviews on the Risks side: the banner points at them, the reviews point back', () => {
    expect(unservedPointers(facts({ unservable: UNSERVABLE, dynamodb_merge_reviews: REVIEWS })))
      .toEqual({ toRisks: 'dynamodb-merge-reviews', toPlan: 'unservable-section' });
  });

  test('one side alone points nowhere', () => {
    expect(unservedPointers(facts({ unservable: UNSERVABLE }))).toEqual({ toRisks: null, toPlan: null });
    expect(unservedPointers(facts({ dynamodb_merge_conflicts: CONFLICTS }))).toEqual({ toRisks: null, toPlan: null });
    expect(unservedPointers({})).toEqual({ toRisks: null, toPlan: null });
  });
});

describe('the pointer line', () => {
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

  test('links the other section through the page and says which tab it is on', () => {
    const goTo = jest.fn();
    act(() => root.render(
      <ResultsSectionNav.Provider value={goTo}>
        <RelatedSectionPointer target="dynamodb-merge-conflicts" t={t} />
      </ResultsSectionNav.Provider>,
    ));
    const link = container.querySelector('a');
    expect(link.getAttribute('href')).toBe('#dynamodb-merge-conflicts');
    expect(container.textContent).toBe(en['analysis-results.related.to-risks']);
    const event = new MouseEvent('click', { bubbles: true, cancelable: true });
    act(() => { link.dispatchEvent(event); });
    expect(goTo).toHaveBeenCalledWith('dynamodb-merge-conflicts');
    expect(event.defaultPrevented).toBe(true);
  });

  test('back to the banner names the Plan tab', () => {
    act(() => root.render(<RelatedSectionPointer target="unservable-section" t={t} />));
    expect(container.textContent).toBe(en['analysis-results.related.to-plan']);
  });

  test('renders nothing without a target', () => {
    act(() => root.render(<RelatedSectionPointer target={null} t={t} />));
    expect(container.innerHTML).toBe('');
  });
});
