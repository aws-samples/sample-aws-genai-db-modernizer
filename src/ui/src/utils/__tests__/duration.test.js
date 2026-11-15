// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { formatDuration } from '../duration';

describe('formatDuration (#2304)', () => {
  it.each([
    [-1, '—'],
    [-3600, '—'],
    [null, '—'],
    [undefined, '—'],
    [NaN, '—'],
    ['abc', '—'],
  ])('reads %p as unknown', (input, expected) => {
    expect(formatDuration(input)).toBe(expected);
  });

  it('formats real durations', () => {
    expect(formatDuration(0)).toBe('0h 0m');
    expect(formatDuration(59)).toBe('0h 0m');
    expect(formatDuration(3725)).toBe('1h 2m');
    expect(formatDuration(90000)).toBe('25h 0m');
  });
});

describe('Dashboard wiring (#2304)', () => {
  const fs = require('fs');
  const path = require('path');
  const source = fs.readFileSync(path.join(__dirname, '../../pages/Dashboard.js'), 'utf8');

  it('uses the shared formatter, not a local copy', () => {
    expect(source).toMatch(/from "\.\.\/utils\/duration"/);
    expect(source).not.toMatch(/const formatDuration = /);
  });

  it('shows no invented average when the API sends none', () => {
    expect(source).not.toMatch(/average_duration_hours \|\| 0/);
    expect(source).toMatch(/average_duration_hours === null \? UNKNOWN_DURATION/);
  });
});
