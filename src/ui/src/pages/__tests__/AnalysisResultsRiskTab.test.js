// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2303: the risk section opens on the highest severity that has an open
 * risk, not on an empty "CRITICAL (0)" tab. The page cannot render under this
 * repo's Jest config (see AnalysisResultsSectionOrder.test.js), so this reads
 * the page source: the risk Tabs is controlled and its default comes from
 * defaultRiskSeverity, which is tested in utils/__tests__/reportFacts.test.js.
 */
const fs = require('fs');
const path = require('path');

const SOURCE = fs.readFileSync(path.join(__dirname, '..', 'AnalysisResults.js'), 'utf8');

test('the risk tabs are controlled and default to the highest open severity', () => {
  const start = SOURCE.indexOf('defaultRiskSeverity(synthesis');
  expect(start).toBeGreaterThan(0);
  const tabsOpen = SOURCE.lastIndexOf('<Tabs', start);
  const block = SOURCE.slice(tabsOpen, start + 200);
  expect(block).toMatch(/activeTabId=/);
  expect(block).toMatch(/onChange=/);
  expect(block).toMatch(/riskTab/);
});
