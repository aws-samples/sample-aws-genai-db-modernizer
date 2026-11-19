// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

// The help site (#2314): mkdocs.yml's site_url, built from docs/ and published by
// .github/workflows/docs.yml. utils/__tests__/helpSite.test.js fails if this URL or a
// page named here drifts from mkdocs.yml and docs/.
export const HELP_SITE_URL = 'https://aws-samples.github.io/sample-aws-genai-db-modernizer/';

/** The URL mkdocs writes for docs/<page>.md (directory URLs), with an optional heading anchor. */
export function helpPageUrl(page, anchor) {
  const dir = String(page).replace(/\.md$/, '');
  return `${HELP_SITE_URL}${dir}/${anchor ? `#${anchor}` : ''}`;
}

// The help that explains the results page: "## The results page" in understand-the-results.md.
export const RESULTS_HELP_URL = helpPageUrl('understand-the-results.md', 'the-results-page');
