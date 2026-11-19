// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// The Cloudscape PropertyFilter's words, from the explorer's shared keys, so every
// filter on the results page reads the same. `ariaLabel` and `placeholder` are the
// caller's own (the filter's name and what it searches).
export function propertyFilterStrings(t, { ariaLabel, placeholder }) {
  const k = (name, opts) => t(`analysis-results.explorer.${name}`, opts);
  return {
    filteringAriaLabel: ariaLabel,
    dismissAriaLabel: k('dismiss-aria-label'),
    clearAriaLabel: k('clear-aria-label'),
    filteringPlaceholder: placeholder,
    groupValuesText: k('group-values-text'),
    groupPropertiesText: k('group-properties-text'),
    operatorsText: k('operators-text'),
    operationAndText: k('operation-and-text'),
    operationOrText: k('operation-or-text'),
    operatorLessText: k('operator-less-text'),
    operatorLessOrEqualText: k('operator-less-or-equal-text'),
    operatorGreaterText: k('operator-greater-text'),
    operatorGreaterOrEqualText: k('operator-greater-or-equal-text'),
    operatorContainsText: k('operator-contains-text'),
    operatorDoesNotContainText: k('operator-does-not-contain-text'),
    operatorEqualsText: k('operator-equals-text'),
    operatorDoesNotEqualText: k('operator-does-not-equal-text'),
    editTokenHeader: k('edit-token-header'),
    propertyText: k('property-text'),
    operatorText: k('operator-text'),
    valueText: k('value-text'),
    cancelActionText: t('common.actions.cancel'),
    applyActionText: k('apply-action-text'),
    allPropertiesLabel: k('all-properties-label'),
    tokenLimitShowMore: k('token-limit-show-more'),
    tokenLimitShowFewer: k('token-limit-show-fewer'),
    clearFiltersText: k('clear-filters-text'),
    removeTokenButtonAriaLabel: (token) => k('remove-token-aria-label', {
      propertyLabel: token.propertyLabel, operator: token.operator, value: token.value,
    }),
    enteredTextLabel: (text) => k('entered-text-label', { text }),
  };
}
