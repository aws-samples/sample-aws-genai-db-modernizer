// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * On a phone, a Cloudscape Tabs that does not fit scrolls its tab list with two icon
 * buttons, which have no name unless i18nStrings gives one (axe button-name at 390 px,
 * found by the both-theme scan of #2311). Every Tabs on the results page names them.
 * The browser check is tests/e2e/test_ui.py::test_every_page_reads_in_both_themes.
 */
import fs from 'fs';
import path from 'path';

const SRC = path.resolve(__dirname, '..', '..');
const FILES = ['pages/AnalysisResults.js', 'components/SourceTablesSection.js', 'components/QueryDetailPanel.js'];

// Each <Tabs ...> opening tag, up to the `tabs=` prop or the end of the tag.
const tabsTags = (text) => [...text.matchAll(/<Tabs\b([\s\S]*?)(?:\/>|\btabs=)/g)].map((m) => m[1]);

test.each(FILES)('every Tabs in %s names its scroll buttons', (file) => {
  // nosemgrep: javascript.lang.security.audit.path-traversal.path-join-resolve-traversal.path-join-resolve-traversal -- path built from the repo's own tree, not input
  const tags = tabsTags(fs.readFileSync(path.join(SRC, file), 'utf8'));
  expect(tags.length).toBeGreaterThan(0);
  tags.forEach((tag) => {
    expect(tag).toContain("scrollLeftAriaLabel: t('analysis-results.tabs.scroll-left')");
    expect(tag).toContain("scrollRightAriaLabel: t('analysis-results.tabs.scroll-right')");
  });
});
