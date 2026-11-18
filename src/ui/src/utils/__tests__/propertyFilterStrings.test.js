// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The results page's property filters (the query table, the close calls) take their
 * words from propertyFilterStrings. Its keys are built by a helper, so the literal-key
 * translation test cannot see them: this one resolves every string with the real
 * locale, so a key dropped from en.json shows as a failure here, not as a raw key on
 * the page ("analysis-results.explorer.clear-filters-text", #2480 review round 2).
 */
import i18n from '../../i18n';
import { propertyFilterStrings } from '../propertyFilterStrings';

const t = i18n.t.bind(i18n);

test('every property filter string resolves to its English text, not its key', () => {
  const strings = propertyFilterStrings(t, { ariaLabel: 'Filter queries', placeholder: 'Find queries' });
  const unresolved = Object.entries(strings)
    .filter(([, value]) => typeof value === 'string')
    .filter(([, value]) => /^[a-z-]+(\.[a-z0-9-]+)+$/.test(value))
    .map(([name, value]) => `${name}: ${value}`);
  expect(unresolved).toEqual([]);
  expect(strings.clearFiltersText).toBe('Clear filters');
  expect(strings.operationAndText).toBe('and');
});

test('the token and entered-text labels fill in their values', () => {
  const strings = propertyFilterStrings(t, { ariaLabel: 'a', placeholder: 'p' });
  expect(strings.removeTokenButtonAriaLabel({ propertyLabel: 'Engine', operator: '=', value: 'DynamoDB' }))
    .toBe('Remove filter: Engine = DynamoDB');
  expect(strings.enteredTextLabel('wp_posts')).toBe('Use: "wp_posts"');
});
