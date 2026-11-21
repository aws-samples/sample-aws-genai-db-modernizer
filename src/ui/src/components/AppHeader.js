// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import React, { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import TopNavigation from '@cloudscape-design/components/top-navigation';
import { LayoutConfigurations } from '../config/GlobalConfigurations';
import { HELP_SITE_URL } from '../utils/helpSite';
import { THEME_CHOICES, applyThemeChoice, onThemeChange, savedThemeChoice } from '../utils/theme';

// The Theme menu's item labels, one locale key per choice.
const THEME_LABEL_KEYS = {
  system: 'common.header.theme-system',
  light: 'common.header.theme-light',
  dark: 'common.header.theme-dark',
};

// `helpHref`: the help page the Help link opens (#2314); the help site's home by default.
function AppHeader({ helpHref = HELP_SITE_URL } = {}) {
  const { t } = useTranslation();
  // #2490: System (the default) follows the OS; Light and Dark are saved in this browser.
  const [choice, setChoice] = useState(savedThemeChoice);
  useEffect(() => onThemeChange(() => setChoice(savedThemeChoice())), []);

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
          // #2314: a text link to the help site, opened in a new tab.
          type: 'button',
          text: t('common.header.help'),
          href: helpHref,
          external: true,
          externalIconAriaLabel: t('common.header.opens-new-tab'),
        },
        {
          type: 'menu-dropdown',
          text: t('common.header.theme'),
          items: THEME_CHOICES.map((id) => ({
            id,
            itemType: 'checkbox',
            checked: choice === id,
            text: t(THEME_LABEL_KEYS[id]),
          })),
          onItemClick: ({ detail }) => applyThemeChoice(detail.id),
        },
      ]}
    />
  );
}

export default AppHeader;
