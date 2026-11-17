// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2346: a request that gets no answer, or runs out of time, resolves with a kind the
 * pages map to a fixed message. A status answer maps through its code. Nothing here is
 * printed; the kind picks the message (utils/requestFailure.js).
 */
import ApiManager from '../ApiManager';
import { failureKind } from '../../utils/requestFailure';

jest.mock('../../config/GlobalConfigurations', () => ({
  ApiConfigurations: { baseUrl: 'http://10.0.0.5:8000/api/v1/', timeout: 1000, headers: {}, mode: 'real' },
}));
jest.mock('../../config/apiMockData', () => ({ getMockResponse: () => ({}) }));

let outcome;
class FakeXhr {
  open() {}
  setRequestHeader() {}
  send() {
    if (outcome.event) { this[outcome.event](); return; }
    this.status = outcome.status;
    this.statusText = 'x';
    this.responseText = outcome.body || '';
    this.onload();
  }
}

beforeEach(() => {
  global.XMLHttpRequest = FakeXhr;
  jest.spyOn(console, 'log').mockImplementation(() => {});
});
afterEach(() => jest.restoreAllMocks());

const run = async () => (await new ApiManager().execute([{ id: 'a', path: 'x', method: 'GET' }])).a;

test('a network failure is unreachable and a timeout is a timeout', async () => {
  outcome = { event: 'onerror' };
  expect(failureKind(await run())).toBe('unreachable');
  outcome = { event: 'ontimeout' };
  expect(failureKind(await run())).toBe('timeout');
});

test('an answer with a status maps through its code, whatever the body says', async () => {
  outcome = { status: 503, body: '<html><script>alert(1)</script> at http://10.0.0.5:8000</html>' };
  const result = await run();
  expect(result.success).toBe(false);
  expect(failureKind(result)).toBe('server');
  outcome = { status: 404, body: JSON.stringify({ detail: 'nope' }) };
  expect(failureKind(await run())).toBe('not-found');
});
