// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The plain-language workload-fit definition appears once in every UI
 * deliverable that shows the routing fit score (bug: Deliverables present a
 * fit score as a confidence percentage).
 *
 * The engineering report and the executive report are covered by
 * `tests/unit/report/test_workload_fit_definition.py`; this file covers the
 * two UI surfaces: the approval gate (`AssignmentGate.js`, via its i18n
 * description), and the live React results page's Query routing help panel
 * (`utils/resultsHelp.js`, #2512), which imports and renders the shared
 * constant directly.
 */
import { WORKLOAD_FIT_DEFINITION } from '../workloadFitLabels';
import en from '../../locales/en.json';

describe('Approval gate states the definition (AssignmentGate.js, via i18n)', () => {
  it('includes the shared definition in the query table description', () => {
    expect(en['assignment-gate.table.description']).toContain(WORKLOAD_FIT_DEFINITION);
  });
});

// #2512: the results page states it in the Query routing help panel, which the query
// table's header and the query detail's workload fit open with their Info links.
describe('React results page states the definition (the Query routing help panel)', () => {
  it('the panel holds the shared workload-fit definition', () => {
    const { helpPanelContent } = require('../resultsHelp');
    const t = key => en[key];
    expect(helpPanelContent('query-routing', t).paragraphs).toContain(WORKLOAD_FIT_DEFINITION);
  });

  it('the query detail links to it instead of repeating it', () => {
    const fs = require('fs');
    const path = require('path');
    const source = fs.readFileSync(
      path.join(__dirname, '..', '..', 'components', 'QueryDetailPanel.js'),
      'utf8',
    );
    expect(source).not.toMatch(/\{WORKLOAD_FIT_DEFINITION\}/);
    expect(source).toMatch(/<HelpInfoLink topic="query-routing"/);
  });
});
