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
  // The default looks at every tab, UNRATED included (a report whose open risks are all unrated).
  expect(block).toMatch(/defaultRiskSeverity\(synthesis, risksBySeverity, riskTabIds\)/);
});

/**
 * #2310: the cards get their look from a class, so the page must pass each card
 * the right tone. The page cannot render here, so these read its source.
 */
test('each risk card takes its tone from the severity status, never a fixed tone', () => {
  const at = SOURCE.indexOf('const status = riskSeverityStatus(severity)');
  expect(at).toBeGreaterThan(0);
  const card = SOURCE.indexOf('<CardBox key={risk.risk_id || idx}', at);
  expect(card).toBeGreaterThan(at);
  expect(SOURCE.slice(card, card + 80)).toContain('tone={status}');
});

test('the query table shows query text in a capped, labelled code block', () => {
  const at = SOURCE.indexOf('{item.query_text}');
  expect(at).toBeGreaterThan(0);
  const open = SOURCE.lastIndexOf('<CodeBlock', at);
  expect(SOURCE.slice(open, at)).toMatch(/<CodeBlock capped label=\{t\('analysis-results\.pattern-detail\.query-text'\)\}>/);
});
