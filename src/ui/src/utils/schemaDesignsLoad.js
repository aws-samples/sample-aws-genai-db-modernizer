// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * True when the Results page could not load the schema designs because a saved
 * design was refused (#2199).
 *
 * `GET /assessments/:jobId/schema-designs` answers 409 when a saved design
 * is refused: an incompatible contract version, or a saved Aurora design
 * whose column type, DDL script, index or foreign key failed its safety
 * check. The page says so with a fixed message and the next step; the API's
 * own text goes to the console only (#2346). Any other failure keeps the
 * old behaviour (no designs, no message).
 *
 * @param {object|undefined} result - the `schemas` entry of `apiManager.execute`
 * @returns {boolean}
 */
export function schemaDesignsRefused(result) {
  return Boolean(result) && !result.success && result.status === 409;
}
