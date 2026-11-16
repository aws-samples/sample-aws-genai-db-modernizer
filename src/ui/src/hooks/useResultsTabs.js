// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { createContext, useCallback, useEffect, useRef, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import {
  hashId, resultsTabFromLocation, searchWithTab, sectionIdsOf, tabOfAnchor, tabParamOf,
} from '../utils/resultsTabs';

// #2313: a jump to a section from inside a tab (e.g. the roadmap's one-step option to
// the target schemas) goes through the page, which opens the section's tab first. The
// value is `goToSection(id)`; outside the results page there is none.
export const ResultsSectionNav = createContext(null);

function scrollToSection(id) {
  const el = document.getElementById(id);
  if (!el) return;
  el.scrollIntoView({ block: 'start' });
  // scrollIntoView leaves focus where it was; a screen reader starts at the section.
  el.setAttribute('tabindex', '-1');
  el.focus({ preventScroll: true });
}

/**
 * The results page's tabs, synced to the URL (#2313).
 *
 * The URL is the one source of the active tab (`resultsTabFromLocation`): `?tab=`, or
 * the tab of the section the hash names. Choosing a tab pushes a history entry
 * (`?tab=<id>`, other parameters kept, the hash dropped since it named a section of the
 * old tab), so back returns to the tab before. A URL whose hash names a section on
 * another tab than `?tab=` (an old anchor, a #2300 `?engine=` link) is rewritten in
 * place to name that tab, then the page scrolls to the section and focuses it once
 * `ready` (the results are on the page). `goToSection(id)` does the same from the page:
 * it pushes a history entry when the section is on another tab, and replaces the entry
 * within the open tab.
 */
export default function useResultsTabs({ ready }) {
  const { search, hash } = useLocation();
  const navigate = useNavigate();
  const activeTab = resultsTabFromLocation(search, hash);
  // The section to scroll to once it is on the page. A new object each time, so going to
  // the same section again scrolls again.
  const [scrollTarget, setScrollTarget] = useState(null);

  useEffect(() => {
    const id = hashId(hash);
    if (id) setScrollTarget({ id });
  }, [hash]);

  // Name the section's tab in the URL, replacing the entry (the link was to the section).
  useEffect(() => {
    const tab = tabOfAnchor(hashId(hash));
    if (tab && tabParamOf(search) !== tab) {
      navigate({ search: searchWithTab(search, tab), hash }, { replace: true });
    }
  }, [search, hash, navigate]);

  useEffect(() => {
    if (!ready || !scrollTarget) return undefined;
    // Wait a frame so the tab the URL just opened is in the DOM.
    const frame = requestAnimationFrame(() => {
      scrollToSection(scrollTarget.id);
      setScrollTarget(current => (current === scrollTarget ? null : current));
    });
    return () => cancelAnimationFrame(frame);
  }, [ready, activeTab, scrollTarget]);

  const selectTab = useCallback((tabId) => {
    navigate({ search: searchWithTab(search, tabId), hash: '' });
  }, [navigate, search]);

  const goToSection = useCallback((id) => {
    const tab = tabOfAnchor(id) || activeTab;
    // A new hash scrolls through the hash effect above; the same one would not fire it.
    if (hashId(hash) === id) setScrollTarget({ id });
    // A jump to another tab is a step the reader can go back from, as choosing the tab
    // is; within the open tab (On this page) it only moves the hash, in place.
    navigate({ search: searchWithTab(search, tab), hash: `#${id}` }, { replace: tab === activeTab });
  }, [navigate, search, hash, activeTab]);

  return { activeTab, selectTab, goToSection };
}

/**
 * The ids of the tab's sections that are on the page, in the tab's order. A section
 * renders only when its data has something to show, and some data arrives after the
 * page, so this watches the content under `root` (an element, from a callback ref:
 * the content is not rendered while the page loads) rather than guessing from the data.
 */
export function useSectionsOnPage(tabId, root) {
  const [present, setPresent] = useState([]);
  const lastKey = useRef('');
  useEffect(() => {
    const ids = sectionIdsOf(tabId);
    const update = () => {
      const next = ids.filter(id => document.getElementById(id));
      const key = next.join(',');
      if (key !== lastKey.current) {
        lastKey.current = key;
        setPresent(next);
      }
    };
    update();
    if (!root || typeof MutationObserver === 'undefined') return undefined;
    const observer = new MutationObserver(update);
    observer.observe(root, { childList: true, subtree: true });
    return () => observer.disconnect();
  }, [tabId, root]);
  return present;
}
