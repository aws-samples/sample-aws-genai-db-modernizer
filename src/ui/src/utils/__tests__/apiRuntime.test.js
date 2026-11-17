// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/** #2327: the new-assessment page reads whether the API runs locally from /health. */
import { healthUrl, readApiLocal } from '../apiRuntime';

test.each([
  ['http://localhost:8000/api/v1/', 'http://localhost:8000/health'],
  ['http://127.0.0.1:8123/api/v1', 'http://127.0.0.1:8123/health'],
  ['/api/v1/', '/health'],
  [undefined, '/health'],
])('the health URL of %s is %s', (base, url) => {
  expect(healthUrl(base)).toBe(url);
});

const answering = (body, ok = true) => jest.fn().mockResolvedValue({ ok, json: async () => body });

test.each([
  [{ status: 'healthy', local: true }, true],
  [{ status: 'healthy', local: false }, false],
  [{ status: 'healthy' }, null],
  [{ status: 'healthy', local: 'yes' }, null],
])('a health body %j reads as %s', async (body, expected) => {
  expect(await readApiLocal('/api/v1/', answering(body))).toBe(expected);
});

test('a failed or unreachable health check says nothing', async () => {
  expect(await readApiLocal('/api/v1/', answering({ local: true }, false))).toBeNull();
  expect(await readApiLocal('/api/v1/', jest.fn().mockRejectedValue(new Error('down')))).toBeNull();
});
