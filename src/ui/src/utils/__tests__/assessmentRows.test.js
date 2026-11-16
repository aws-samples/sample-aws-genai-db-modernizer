// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import fs from 'fs';
import path from 'path';
import { assessmentHref, databaseLabel, copyJobIdLabel } from '../assessmentRows';
import en from '../../locales/en.json';

describe('assessmentHref (#2305)', () => {
  it('opens a finished job on its results page', () => {
    expect(assessmentHref('abc12345', 'SUCCEEDED')).toBe('/analysis/results/abc12345');
    expect(assessmentHref('abc12345', 'COMPLETED')).toBe('/analysis/results/abc12345');
  });

  it.each(['RUNNING', 'WAITING', 'PENDING', 'FAILED', 'CANCELLED', undefined])(
    'opens a %s job on its progress page',
    (status) => {
      expect(assessmentHref('abc12345', status)).toBe('/analysis/monitor/summary/abc12345');
    },
  );
});

describe('databaseLabel (#2305)', () => {
  it('names the row by its database', () => {
    expect(databaseLabel('wordpress', 'abc12345')).toBe('wordpress');
  });

  it('falls back to the job id when the API has no database name', () => {
    expect(databaseLabel(null, 'abc12345')).toBe('abc12345');
    expect(databaseLabel('', 'abc12345')).toBe('abc12345');
  });
});

describe('Dashboard table (#2305)', () => {
  const source = fs.readFileSync(path.join(__dirname, '../../pages/Dashboard.js'), 'utf8');

  it('leads with the database column and keeps the job id second', () => {
    expect(source).toMatch(/useState\(\['databaseName', 'jobId', 'status'/);
  });

  it('links a row through assessmentHref and wraps lines', () => {
    expect(source).toMatch(/assessmentHref\(item\.jobId, item\.status\)/);
    expect(source).toMatch(/wrapLines: true/);
  });
});

test('Dashboard imports the helpers it uses (#2305)', () => {
  const source = fs.readFileSync(path.join(__dirname, '../../pages/Dashboard.js'), 'utf8');
  expect(source).toMatch(/import \{ assessmentHref, copyJobIdLabel, databaseLabel \} from "\.\.\/utils\/assessmentRows"/);
});

describe('copyJobIdLabel (#2340 review)', () => {
  const t = (key, values) => en[key].replace('{{name}}', values.name).replace('{{id}}', values.id);

  it('gives each row its own accessible name, even for two jobs on one database', () => {
    const a = copyJobIdLabel(t, 'wordpress', 'b4f93edf');
    const b = copyJobIdLabel(t, 'wordpress', '22222222');
    expect(a).toBe('Copy job ID for wordpress (b4f93edf)');
    expect(a).not.toBe(b);
  });

  it('names the job id when the API has no database name', () => {
    expect(copyJobIdLabel(t, null, 'b4f93edf')).toBe('Copy job ID for b4f93edf (b4f93edf)');
  });

  it('the Dashboard uses it for every row', () => {
    const source = fs.readFileSync(path.join(__dirname, '../../pages/Dashboard.js'), 'utf8');
    expect(source).toMatch(/copyButtonAriaLabel=\{copyJobIdLabel\(t, item\.databaseName, item\.jobId\)\}/);
    expect(source).not.toMatch(/copyButtonAriaLabel=\{t\('common\.actions\.copy-job-id'\)\}/);
  });
});
