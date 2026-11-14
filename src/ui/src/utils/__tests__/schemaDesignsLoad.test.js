// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2199: a refused saved design (409 from /schema-designs) reaches the user
 * as the API's message on the Results page, instead of being dropped.
 */
import fs from 'fs';
import path from 'path';
import { errorFromResponse } from '../apiErrors';
import { schemaDesignsRefusal } from '../schemaDesignsLoad';

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

test('a 409 gives the API detail, with its next step', () => {
  expect(schemaDesignsRefusal(failed(409, DETAIL))).toBe(DETAIL);
});

test('a loaded response, a missing entry and other failures give nothing', () => {
  expect(schemaDesignsRefusal({ success: true, status: 200, schema_designs: [] })).toBeNull();
  expect(schemaDesignsRefusal(undefined)).toBeNull();
  expect(schemaDesignsRefusal(failed(404, 'Not found'))).toBeNull();
  expect(schemaDesignsRefusal({ success: false, error: new Error('Network error') })).toBeNull();
});

test('the Results page shows the refusal when it loads the schema designs', () => {
  const page = fs.readFileSync(path.join(__dirname, '../../pages/AnalysisResults.js'), 'utf8');
  const load = page.slice(page.indexOf("id: 'schemas'"), page.indexOf("results['collector']?.success"));
  expect(load).toMatch(/schemaDesignsRefusal\(results\['schemas'\]\)/);
  expect(load).toMatch(/addFlashbarMessage\(\{ type: 'error'[^}]*content: designsRefused/);
});
