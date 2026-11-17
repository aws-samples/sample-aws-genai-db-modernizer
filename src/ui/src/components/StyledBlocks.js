// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Cards and code blocks for the results and review pages (#2310).
 *
 * Cloudscape components forward only id, className and data-* attributes, so a
 * `style` prop on a Box is lost. The look lives in a wrapper element with a
 * class, and the colours in styles/global.css come from theme variables, so
 * both the light and the dark theme read well.
 */
import React from 'react';
import Box from '@cloudscape-design/components/box';

/**
 * A padded card with a coloured left border. `tone` picks the border and
 * background: info, note, danger, or a risk severity (error, warning,
 * success, stopped).
 */
export function CardBox({ tone = 'info', children }) {
  return (
    <div className={`dbm-card dbm-card--${tone}`}>
      <Box padding={{ vertical: 'xs', horizontal: 's' }}>{children}</Box>
    </div>
  );
}

/**
 * Monospace text in a scrollable block that keeps line breaks. A capped block
 * scrolls, so with a `label` (pass a translated string) it is a focusable
 * region that keyboard users can scroll. There is no built-in label text.
 */
export function CodeBlock({ capped = false, label, children }) {
  // A region needs a name, and the caller owns the translated text. With no label the block still scrolls but is not offered as a region.
  const scrollProps = capped && label ? { tabIndex: 0, role: 'region', 'aria-label': label } : {};
  return (
    <div className={`dbm-code-block${capped ? ' dbm-code-block--capped' : ''}`} {...scrollProps}>
      <Box fontSize="body-s" fontFamily="monospace" padding="s">{children}</Box>
    </div>
  );
}
