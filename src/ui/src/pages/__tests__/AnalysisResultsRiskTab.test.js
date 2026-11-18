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
  const card = SOURCE.indexOf('<RiskCard');
  expect(card).toBeGreaterThan(0);
  expect(SOURCE.slice(card, card + 400)).toContain('severity={severity}');
  // No unused tone variable is left on the page: the card derives its own.
  expect(SOURCE).not.toContain('const status = riskSeverityStatus(severity)');
});

/** #2322: the card derives its tone from the severity it is given, never a fixed one. */
test('the risk card takes its tone class from the severity status', () => {
  const card = fs.readFileSync(path.join(__dirname, '..', '..', 'components', 'RiskCard.js'), 'utf8');
  expect(card).toContain('dbm-card--${status}');
  expect(card).toContain('riskSeverityStatus(severity)');
});

test('the query table shows query text in a capped, labelled code block', () => {
  const at = SOURCE.indexOf('{item.query_text}');
  expect(at).toBeGreaterThan(0);
  const open = SOURCE.lastIndexOf('<CodeBlock', at);
  expect(SOURCE.slice(open, at)).toMatch(/<CodeBlock capped label=\{t\('analysis-results\.pattern-detail\.query-text'\)\}>/);
});

/** #2322: severities read in sentence case through the locale file, counts from the facts. */
test('the tab labels, overall risk and resolved risks say the severity through its label', () => {
  expect(SOURCE).toMatch(/tab-label', \{ severity: riskSeverityLabel\(severity, t\), count: severityCount \}/);
  expect(SOURCE).toMatch(/overall-risk', \{ severity: riskSeverityLabel\(overallRiskLevel, t\)\.toLowerCase\(\) \}/);
  expect(SOURCE).toMatch(/\{riskSeverityLabel\(risk\.severity, t\)\}<\/StatusIndicator>/);
  expect(SOURCE).not.toMatch(/>\{severity\}<\/StatusIndicator>/);
  expect(SOURCE).not.toMatch(/text-transform|textTransform: ?['"]uppercase/);
});

test('the tab count comes from the facts and the risk cards are keyed and opened by risk key', () => {
  expect(SOURCE).toMatch(/openRiskCountAt\(synthesis, severity, risksAtSeverity\)/);
  expect(SOURCE).toMatch(/const key = riskKey\(severity, risk, idx\)/);
  expect(SOURCE).toMatch(/expanded=\{riskOpen\[key\] \?\? false\}/);
  expect(SOURCE).toMatch(/headingLevel="h3"/);
});
