// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2199: a refused saved design (409 from /schema-designs) shows on the Results
 * page, as the API's message, instead of being dropped. #2346: the message is shown
 * only through failureReason (4xx, plain string, short, nothing technical in it).
 */
import fs from 'fs';
import path from 'path';
import { errorFromResponse } from '../apiErrors';
import { schemaDesignsRefused } from '../schemaDesignsLoad';
import { failureReason } from '../requestFailure';

const DETAIL =
  "This assessment's saved Aurora MySQL design failed its safety check: it has an index "
  + 'definition the tool does not write, so it was not loaded. Rerun the Aurora MySQL schema design.';

function failed(status, detail) {
  return {
    id: 'schemas',
    success: false,
    status,
    error: errorFromResponse(status, 'Conflict', JSON.stringify({ detail })),
  };
}

test('a 409 is a refused design and gives the API detail, with its next step', () => {
  expect(schemaDesignsRefused(failed(409, DETAIL))).toBe(true);
  expect(failureReason(failed(409, DETAIL))).toBe(DETAIL);
});

test('a refusal whose detail is unsafe still shows, with the fixed next step (#2346)', () => {
  const en = require('../../locales/en.json');
  const hostile = failed(409, '<b>refused</b> at https://10.0.0.5:8000/x');
  expect(schemaDesignsRefused(hostile)).toBe(true);
  expect(failureReason(hostile)).toBeNull();
  expect(en['analysis-results.error.schema-designs-refused-detail']).toContain('Run the schema design again');
});

test('a loaded response, a missing entry and other failures give nothing', () => {
  expect(schemaDesignsRefused({ success: true, status: 200, schema_designs: [] })).toBe(false);
  expect(schemaDesignsRefused(undefined)).toBe(false);
  expect(schemaDesignsRefused(failed(404, 'Not found'))).toBe(false);
  expect(schemaDesignsRefused({ success: false, error: new Error('Network error') })).toBe(false);
});

test('the Results page shows the refusal when it loads the schema designs', () => {
  const page = fs.readFileSync(path.join(__dirname, '../../pages/AnalysisResults.js'), 'utf8');
  const load = page.slice(page.indexOf("id: 'schemas'"), page.indexOf("results['collector']?.success"));
  expect(load).toMatch(/schemaDesignsRefused\(results\['schemas'\]\)/);
  expect(load).toMatch(/addFlashbarMessage\(\{ type: 'error'[^}]*failureReason\(results\['schemas'\], t\)[^}]*schema-designs-refused-detail/);
});
