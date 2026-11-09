// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #1991 review: the API refuses a POST without `Content-Type: application/json` (415) on
 * the routes that take no body (the schema confirm routes, the upload confirm) and on the
 * assumptions panel's, so a cross-site form post can't reach them. The UI's own calls
 * go through ApiManager, which sends that header and a JSON body on every POST.
 */
import ApiManager from '../ApiManager';

jest.mock('../../config/GlobalConfigurations', () => ({
  ApiConfigurations: {
    baseUrl: 'http://localhost:8000/api/v1/',
    timeout: 1000,
    headers: { 'Content-Type': 'application/json' },
    mode: 'real',
  },
}));
jest.mock('../../config/apiMockData', () => ({ getMockResponse: () => ({}) }));

let sent;

class FakeXhr {
  constructor() { this.headers = {}; sent.push(this); }
  open(method, url) { this.method = method; this.url = url; }
  setRequestHeader(k, v) { this.headers[k] = v; }
  send(body) {
    this.body = body;
    this.status = 200;
    this.responseText = '{}';
    this.onload();
  }
}

beforeEach(() => {
  sent = [];
  global.XMLHttpRequest = FakeXhr;
});

test.each([
  'assessments/job-1/uploads/confirm?database_name=shop',
  'assessments/job-1/schema/confirm-all?database_name=shop',
  'assessments/job-1/schema/dynamodb/confirm?database_name=shop',
  'assessments/job-1/assumptions',
])('a POST to %s sends a JSON content type and body', async (path) => {
  await new ApiManager().execute([{ id: 'c', path, method: 'POST', params: {} }]);
  expect(sent).toHaveLength(1);
  expect(sent[0].headers['Content-Type']).toBe('application/json');
  expect(sent[0].body).toBe('{}');
});

test('a JSON content type is sent on a POST with no params', async () => {
  await new ApiManager().execute([{ id: 'c', path: 'assessments/job-1/schema/confirm-all', method: 'POST' }]);
  expect(sent[0].headers['Content-Type']).toBe('application/json');
  expect(sent[0].body).toBe('{}');
});
