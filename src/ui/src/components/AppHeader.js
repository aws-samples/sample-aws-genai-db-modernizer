// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import TopNavigation from '@cloudscape-design/components/top-navigation';
import { applyMode, Mode } from '@cloudscape-design/global-styles';
import { LayoutConfigurations } from '../config/GlobalConfigurations';

function AppHeader() {
  const { t } = useTranslation();
  const [isDark, setIsDark] = useState(
    (localStorage.getItem('dbm-theme') || 'dark') === 'dark'
  );

  const toggleTheme = () => {
    const newTheme = isDark ? 'light' : 'dark';
    localStorage.setItem('dbm-theme', newTheme);
    applyMode(newTheme === 'light' ? Mode.Light : Mode.Dark);
    setIsDark(!isDark);
    // Notify components that read theme from localStorage (e.g. ProgressState)
    window.dispatchEvent(new Event('dbm-theme-change'));
  };

  return (
    <TopNavigation
      identity={{
        href: '/',
        title: LayoutConfigurations['application-title'],
      }}
      // At a narrow width the utilities fold into a menu; its button and panel need names (#2312).
      i18nStrings={{
        overflowMenuTriggerText: t('common.header.more'),
        overflowMenuTitleText: t('common.header.menu-title'),
        overflowMenuBackIconAriaLabel: t('common.header.menu-back'),
        overflowMenuDismissIconAriaLabel: t('common.header.menu-close'),
      }}
      utilities={[
        {
          type: 'button',
          iconName: isDark ? 'star' : 'star-filled',
          title: isDark ? 'Switch to light theme' : 'Switch to dark theme',
          // An icon-only utility: the accessible name is the same text (axe: aria-command-name).
          ariaLabel: isDark ? 'Switch to light theme' : 'Switch to dark theme',
          onClick: toggleTheme,
        },
      ]}
    />
  );
}

export default AppHeader;
