// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2307: every job route (results, progress, assignment) shows one not-found
 * state when the assessment does not exist, and never mounts its page, so no
 * request URL, status code, skeleton or approve button reaches the reader.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import JobRouteGuard from '../JobRouteGuard';

const mockExecute = jest.fn();
jest.mock('../../classes/ApiManager', () => ({
  __esModule: true,
  default: function ApiManager() {
    this.execute = mockExecute;
  },
}));
jest.mock('../AssessmentNotFound', () => ({
  __esModule: true,
  default: ({ jobId }) => <div data-testid="not-found" data-job-id={jobId} />,
  AssessmentLookupPending: () => <div data-testid="pending" role="status" />,
}));

global.IS_REACT_ACT_ENVIRONMENT = true;

let container;
let root;

async function renderAt(path) {
  await act(async () => {
    root.render(
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route
            path="/analysis/results/:jobId"
            element={<JobRouteGuard><div data-testid="page" /></JobRouteGuard>}
          />
        </Routes>
      </MemoryRouter>,
    );
  });
  await act(async () => {});
}

const has = (id) => container.querySelector(`[data-testid="${id}"]`) !== null;

beforeEach(() => {
  mockExecute.mockReset();
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

test('an unknown id shows the not-found state and never mounts the page', async () => {
  mockExecute.mockResolvedValue({ job: { success: false, status: 404 } });
  await renderAt('/analysis/results/doesnotexist');
  expect(has('not-found')).toBe(true);
  expect(container.querySelector('[data-testid="not-found"]').getAttribute('data-job-id')).toBe('doesnotexist');
  expect(has('page')).toBe(false);
  expect(mockExecute.mock.calls[0][0][0].path).toBe('assessments/doesnotexist');
});

test('a known id mounts the page', async () => {
  mockExecute.mockResolvedValue({ job: { success: true, status: 200 } });
  await renderAt('/analysis/results/abc12345');
  expect(has('page')).toBe(true);
  expect(has('not-found')).toBe(false);
});

test('the page is not mounted while the lookup is pending, and a loading status shows', async () => {
  mockExecute.mockReturnValue(new Promise(() => {}));
  await renderAt('/analysis/results/abc12345');
  expect(has('page')).toBe(false);
  expect(has('not-found')).toBe(false);
  expect(has('pending')).toBe(true);
});

test('a 422 from the API is not found too: no assessment can have that id', async () => {
  mockExecute.mockResolvedValue({ job: { success: false, status: 422 } });
  await renderAt('/analysis/results/abc12345');
  expect(has('not-found')).toBe(true);
  expect(has('page')).toBe(false);
});

// The API only accepts 1 to 128 ASCII letters, digits, underscores and hyphens
// (src/shared/job_id.py), so any other id is not found without asking it.
test.each([
  ['an id with dots', 'does.not.exist'],
  ['an id with a space', 'abc%20def'],
  ['a 129-character id', 'a'.repeat(129)],
])('%s is not found without a lookup', async (_name, id) => {
  await renderAt(`/analysis/results/${id}`);
  expect(has('not-found')).toBe(true);
  expect(has('page')).toBe(false);
  expect(mockExecute).not.toHaveBeenCalled();
});

test('a 128-character id is still looked up', async () => {
  mockExecute.mockResolvedValue({ job: { success: true, status: 200 } });
  await renderAt(`/analysis/results/${'a'.repeat(128)}`);
  expect(mockExecute).toHaveBeenCalledTimes(1);
  expect(has('page')).toBe(true);
});

test.each([
  ['a network error', { job: { success: false, error: new Error('Network error') } }],
  ['a server error', { job: { success: false, status: 500 } }],
])('%s is not "not found": the page mounts and shows its own error', async (_name, response) => {
  mockExecute.mockResolvedValue(response);
  await renderAt('/analysis/results/abc12345');
  expect(has('page')).toBe(true);
  expect(has('not-found')).toBe(false);
});

test('a lookup that throws is not "not found" either', async () => {
  mockExecute.mockRejectedValue(new Error('boom'));
  await renderAt('/analysis/results/abc12345');
  expect(has('page')).toBe(true);
});
