// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Whether the API keeps uploads on this machine (#2327), from the `local` field of its
 * `GET /health`. The health route sits at the API's root, next to `api/v1/`.
 */

/** The health URL for an API base URL such as `http://localhost:8000/api/v1/` or `/api/v1/`. */
export function healthUrl(baseUrl) {
  const base = String(baseUrl || '/api/v1/');
  const root = base.replace(/api\/v1\/?$/, '');
  return `${root.endsWith('/') ? root : `${root}/`}health`;
}

/** true or false as the API says; null when it cannot be reached or does not say. */
export async function readApiLocal(baseUrl, fetchImpl = (...args) => window.fetch(...args)) {
  try {
    const response = await fetchImpl(healthUrl(baseUrl), { headers: { Accept: 'application/json' } });
    if (!response.ok) {
      return null;
    }
    const body = await response.json();
    return typeof body?.local === 'boolean' ? body.local : null;
  } catch {
    return null;
  }
}
