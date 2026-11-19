// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2314: the Help link in the top navigation opens the help site, and the results
 * page's opens the page that explains it. The site is built from mkdocs.yml and
 * docs/; these tests fail when the URL or the page it names drifts from them.
 */
import fs from 'fs';
import path from 'path';
import { HELP_SITE_URL, RESULTS_HELP_URL, helpPageUrl } from '../helpSite';

const REPO = path.resolve(__dirname, '../../../../..');
const MKDOCS = fs.readFileSync(path.join(REPO, 'mkdocs.yml'), 'utf8');

test('the help site URL is the site_url mkdocs.yml publishes', () => {
  const siteUrl = MKDOCS.match(/^site_url:\s*(\S+)\s*$/m)[1];
  expect(HELP_SITE_URL).toBe(siteUrl);
  expect(HELP_SITE_URL.startsWith('https://')).toBe(true);
});

test('a page URL is the site URL plus the page directory, as mkdocs writes it', () => {
  expect(helpPageUrl('understand-the-results.md')).toBe(`${HELP_SITE_URL}understand-the-results/`);
  expect(helpPageUrl('understand-the-results.md', 'the-results-page')).toBe(
    `${HELP_SITE_URL}understand-the-results/#the-results-page`,
  );
});

test('the results page help names a page in the site navigation and a heading on it', () => {
  expect(RESULTS_HELP_URL).toBe(helpPageUrl('understand-the-results.md', 'the-results-page'));
  // The page is in the nav, so the site builds it (use_directory_urls is the default).
  expect(MKDOCS).toMatch(/^\s*- [^:\n]+: understand-the-results\.md\s*$/m);
  expect(MKDOCS).not.toMatch(/use_directory_urls:\s*false/);
  const page = fs.readFileSync(path.join(REPO, 'docs', 'understand-the-results.md'), 'utf8');
  // mkdocs slugs "## The results page" to #the-results-page.
  expect(page).toMatch(/^## The results page$/m);
});
