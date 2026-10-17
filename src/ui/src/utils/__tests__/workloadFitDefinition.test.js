/**
 * The plain-language workload-fit definition appears once in every UI
 * deliverable that shows the routing fit score (bug: Deliverables present a
 * fit score as a confidence percentage).
 *
 * The engineering report and the executive report are covered by
 * `tests/unit/report/test_workload_fit_definition.py`; this file covers the
 * two UI surfaces: the approval gate (`AssignmentGate.js`, via its i18n
 * description), and the live React results page's query-journey detail
 * (`AnalysisResults.js`), which imports and renders the shared constant
 * directly.
 */
import { WORKLOAD_FIT_DEFINITION } from '../workloadFitLabels';
import en from '../../locales/en.json';

describe('Approval gate states the definition (AssignmentGate.js, via i18n)', () => {
  it('includes the shared definition in the query table description', () => {
    expect(en['assignment-gate.table.description']).toContain(WORKLOAD_FIT_DEFINITION);
  });
});

describe('React results page states the definition (AnalysisResults.js)', () => {
  it('imports and renders the shared workload-fit definition', async () => {
    const fs = require('fs');
    const path = require('path');
    const source = fs.readFileSync(
      path.join(__dirname, '..', '..', 'pages', 'AnalysisResults.js'),
      'utf8',
    );
    expect(source).toMatch(/import\s*{\s*WORKLOAD_FIT_DEFINITION\s*}\s*from\s*"\.\.\/utils\/workloadFitLabels"/);
    expect(source).toMatch(/WORKLOAD_FIT_DEFINITION/);
  });
});
