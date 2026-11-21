// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { createContext, useCallback, useEffect, useRef, useState } from 'react';
import {
  DEFAULT_HELP_TOPIC, HELP_DRAWER_ID, ON_THIS_PAGE_DRAWER_ID, helpTopicOf,
} from '../utils/resultsHelp';

// #2512: `openHelp(topic)` for the results page's Info links. Outside the results page
// there is none, and an Info link renders nothing.
export const ResultsHelp = createContext(null);

// "On this page" opens with the page on a wide screen; on a narrow one it would cover it.
function wideScreen() {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function'
    && window.matchMedia('(min-width: 1400px)').matches;
}

/**
 * The results page's two drawers in the AppLayout tools area (#2512): "On this page"
 * (#2313) and the help panel. One drawer is open at a time. `openHelp(topic)` shows a
 * topic in the help panel, opens it and moves focus into it (Cloudscape's Info link
 * pattern), also when the panel is already open on another topic. `layoutRef` goes on
 * the AppLayout, which focuses its active drawer.
 */
export default function useResultsDrawers() {
  const [activeDrawerId, setActiveDrawerId] = useState(() => (wideScreen() ? ON_THIS_PAGE_DRAWER_ID : null));
  const [helpTopic, setHelpTopic] = useState(DEFAULT_HELP_TOPIC);
  const [focusRequest, setFocusRequest] = useState(0);
  const layoutRef = useRef(null);

  const openHelp = useCallback((topic) => {
    setHelpTopic(helpTopicOf(topic));
    setActiveDrawerId(HELP_DRAWER_ID);
    setFocusRequest(n => n + 1);
  }, []);

  useEffect(() => {
    if (!focusRequest) return undefined;
    // The drawer renders its new content on the next frame; focus it then.
    const frame = requestAnimationFrame(() => {
      if (layoutRef.current && typeof layoutRef.current.focusActiveDrawer === 'function') {
        layoutRef.current.focusActiveDrawer();
      }
    });
    return () => cancelAnimationFrame(frame);
  }, [focusRequest]);

  const onDrawerChange = useCallback(({ detail }) => setActiveDrawerId(detail.activeDrawerId), []);

  return { activeDrawerId, helpTopic, openHelp, onDrawerChange, layoutRef };
}

/**
 * The AppLayout `drawers`: "On this page" when the open tab lists two sections or more
 * (`onThisPage` is its content, or null), then the help panel, always.
 */
export function resultsDrawers({ t, onThisPage, help }) {
  const drawers = [];
  if (onThisPage) {
    drawers.push({
      id: ON_THIS_PAGE_DRAWER_ID,
      content: onThisPage,
      trigger: { iconName: 'menu' },
      ariaLabels: {
        drawerName: t('common.layout.tools'),
        triggerButton: t('common.layout.tools-toggle'),
        closeButton: t('common.layout.tools-close'),
      },
    });
  }
  drawers.push({
    id: HELP_DRAWER_ID,
    content: help,
    trigger: { iconName: 'status-info' },
    ariaLabels: {
      drawerName: t('analysis-results.help.drawer-name'),
      triggerButton: t('analysis-results.help.drawer-open'),
      closeButton: t('analysis-results.help.drawer-close'),
    },
  });
  return drawers;
}

/** The drawer to show: the chosen one while the page has it, otherwise none. */
export function shownDrawerId(activeDrawerId, drawers) {
  return drawers.some(d => d.id === activeDrawerId) ? activeDrawerId : null;
}
