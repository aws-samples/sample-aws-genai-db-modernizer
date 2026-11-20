// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The app's own CSS custom properties (--dbm-*), each set to a Cloudscape design token
 * (#2311). The CSS files read var(--dbm-...); a token is itself a CSS variable that
 * Cloudscape switches between the light and dark themes, so every rule follows the
 * theme with no dark-mode copy of its own.
 *
 * The properties are declared on <body>, the element Cloudscape sets the tokens on (and
 * where applyMode adds awsui-dark-mode), so each resolves to the current theme's value.
 * A plain CSS file cannot import the tokens package, which is why this list is
 * JavaScript and index.js writes it into the page as one style element.
 */
import {
  colorBackgroundBadgeGrey,
  colorBackgroundButtonPrimaryDefault,
  colorBackgroundCellShaded,
  colorBackgroundCodeView,
  colorBackgroundContainerContent,
  colorBackgroundDropdownItemHover,
  colorBackgroundItemSelected,
  colorBackgroundLayoutMain,
  colorBackgroundStatusError,
  colorBackgroundStatusInfo,
  colorBackgroundStatusWarning,
  colorBorderDividerDefault,
  colorBorderDividerSecondary,
  colorBorderItemFocused,
  colorBorderStatusError,
  colorBorderStatusInfo,
  colorBorderStatusSuccess,
  colorBorderStatusWarning,
  colorTextAccent,
  colorTextBadgeGrey,
  colorTextBodyDefault,
  colorTextBodySecondary,
  colorTextButtonPrimaryDefault,
  colorTextLinkDefault,
  colorTextLinkHover,
  colorTextStatusInactive,
  colorTextStatusWarning,
} from '@cloudscape-design/design-tokens';

export const THEME_VARS = {
  // The page itself.
  '--dbm-page-bg': colorBackgroundLayoutMain,
  '--dbm-text-primary': colorTextBodyDefault,
  '--dbm-text-secondary': colorTextBodySecondary,
  '--dbm-text-muted': colorTextStatusInactive,
  '--dbm-text-link': colorTextLinkDefault,
  '--dbm-text-link-hover': colorTextLinkHover,
  '--dbm-text-code': colorTextAccent,
  '--dbm-border-subtle': colorBorderDividerDefault,
  '--dbm-border-divider': colorBorderDividerSecondary,
  '--dbm-focus-ring': colorBorderItemFocused,
  '--dbm-bg-surface': colorBackgroundCellShaded,
  '--dbm-bg-surface-hover': colorBackgroundDropdownItemHover,
  '--dbm-bg-code': colorBackgroundCodeView,
  '--dbm-bg-highlight': colorBackgroundItemSelected,
  '--dbm-signal-badge-muted-bg': colorBackgroundCellShaded,
  // A query the review moved or changed: Cloudscape's warning colours, which keep
  // 4.5:1 in both themes (the old orange on its own tint did not in the light theme).
  '--dbm-moved-text': colorTextStatusWarning,
  '--dbm-moved-bg': colorBackgroundStatusWarning,
  '--dbm-moved-border': colorBorderStatusWarning,
  '--dbm-cache-bg': colorBackgroundStatusInfo,
  // Cards (#2310): an accent edge on a tinted or plain container.
  '--dbm-card-info-border': colorBorderStatusInfo,
  '--dbm-card-info-bg': colorBackgroundStatusInfo,
  '--dbm-card-note-border': colorBorderStatusWarning,
  '--dbm-card-note-bg': colorBackgroundStatusWarning,
  '--dbm-card-danger-border': colorBorderStatusError,
  '--dbm-card-danger-bg': colorBackgroundStatusError,
  '--dbm-card-error-border': colorBorderStatusError,
  '--dbm-card-warning-border': colorBorderStatusWarning,
  '--dbm-card-success-border': colorBorderStatusSuccess,
  '--dbm-card-stopped-border': colorTextStatusInactive,
  '--dbm-card-neutral-bg': colorBackgroundContainerContent,
  '--dbm-code-bg': colorBackgroundCodeView,
  '--dbm-code-text': colorTextBodyDefault,
  // Query id chips (#2323): Cloudscape's grey badge, and the primary button on hover.
  '--dbm-id-chip-bg': colorBackgroundBadgeGrey,
  '--dbm-id-chip-text': colorTextBadgeGrey,
  '--dbm-id-chip-hover-bg': colorBackgroundButtonPrimaryDefault,
  '--dbm-id-chip-hover-text': colorTextButtonPrimaryDefault,
};

export const themeVarsCss = () => `body {\n${Object.entries(THEME_VARS)
  .map(([name, value]) => `  ${name}: ${value};`)
  .join('\n')}\n}\n`;

// Write the properties into the page once; a second call replaces the first.
export function installThemeVars(doc = document) {
  let style = doc.getElementById('dbm-theme-vars');
  if (!style) {
    style = doc.createElement('style');
    style.id = 'dbm-theme-vars';
    doc.head.appendChild(style);
  }
  style.textContent = themeVarsCss();
  return style;
}
