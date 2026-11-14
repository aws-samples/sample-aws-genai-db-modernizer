// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * Why the Results page could not load the schema designs, or `null` (#2199).
 *
 * `GET /assessments/:jobId/schema-designs` answers 409 when a saved design
 * is refused: an incompatible contract version, or a saved Aurora design
 * whose column type, DDL script, index or foreign key failed its safety
 * check. The API's `detail` (already the Error's message, see
 * `apiErrors.js`) says what is wrong and what to do, with no path and no
 * stored text. Before this the page dropped the failed fetch silently and
 * rendered the Aurora section with no DDL. Any other failure keeps the
 * old behaviour (no designs, no message).
 *
 * @param {object|undefined} result - the `schemas` entry of `apiManager.execute`
 * @returns {string|null} the message to show, or null
 */
export function schemaDesignsRefusal(result) {
  if (!result || result.success || result.status !== 409) {
    return null;
  }
  return result.error?.message || null;
}
