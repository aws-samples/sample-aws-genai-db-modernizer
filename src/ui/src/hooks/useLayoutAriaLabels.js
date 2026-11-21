// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { useMemo } from 'react';
import { useTranslation } from 'react-i18next';

/**
 * The names Cloudscape's AppLayout gives its navigation and tools panels and their toggle
 * buttons (#2312). Every page passes this one object as `ariaLabels`: without it the toggles
 * have no accessible name (axe `button-name`, critical) and the two landmarks are
 * unnamed (`landmark-unique`).
 */
export default function useLayoutAriaLabels() {
  const { t } = useTranslation();
  return useMemo(() => ({
    navigation: t('common.layout.navigation'),
    navigationToggle: t('common.layout.navigation-toggle'),
    navigationClose: t('common.layout.navigation-close'),
    tools: t('common.layout.tools'),
    toolsToggle: t('common.layout.tools-toggle'),
    toolsClose: t('common.layout.tools-close'),
    // #2512: the results page's drawers (On this page and the help panel).
    drawers: t('common.layout.drawers'),
    drawersOverflow: t('common.layout.drawers-overflow'),
    drawersOverflowWithBadge: t('common.layout.drawers-overflow'),
  }), [t]);
}
