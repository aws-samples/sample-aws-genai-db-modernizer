// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2307: the job route guard decides "not found" from the id's shape without
 * asking the API. That shape must be the API's own rule
 * (`JOB_ID_PATTERN` in src/shared/job_id.py); if one changes without the
 * other, an id the API accepts would show "not found", or an id it refuses
 * would reach the page. This test reads the Python rule and compares.
 */
import fs from 'fs';
import path from 'path';
import { JOB_ID_SHAPE, isWellFormedJobId } from '../jobId';

const PYTHON_RULE = fs.readFileSync(
  path.resolve(__dirname, '../../../../shared/job_id.py'),
  'utf8',
);

function pythonPattern() {
  const match = PYTHON_RULE.match(/^JOB_ID_PATTERN = r"([^"]+)"$/m);
  expect(match).not.toBeNull();
  return match[1];
}

test("the UI's id shape is src/shared/job_id.py's pattern, anchored", () => {
  expect(JOB_ID_SHAPE.source).toBe(`^${pythonPattern()}$`);
});

test('both rules agree on ids at the edges', () => {
  const python = new RegExp(`^${pythonPattern()}$`);
  const ids = [
    'abc12345', 'a', 'a'.repeat(128), 'a'.repeat(129), '', 'does.not.exist', '.', '..',
    'abc def', 'ab/cd', 'ab_cd-ef', 'ÄÖ', 'job_2307-sample',
  ];
  ids.forEach((id) => {
    expect([id.length, isWellFormedJobId(id)]).toEqual([id.length, python.test(id)]);
  });
});

test('an absent id is not well formed', () => {
  expect(isWellFormedJobId(undefined)).toBe(false);
  expect(isWellFormedJobId(null)).toBe(false);
});

test('the guard uses the shared rule, not a copy', () => {
  const guard = fs.readFileSync(path.resolve(__dirname, '../../components/JobRouteGuard.js'), 'utf8');
  expect(guard).toMatch(/from '\.\.\/utils\/jobId'/);
  expect(guard).not.toMatch(/A-Za-z0-9_-/);
});
