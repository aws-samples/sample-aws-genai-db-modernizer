// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The experimental graph explorer's probe (#308), on its own so the results
 * page can ask whether the explorer is on without loading the explorer.
 */

export const EXPLORER_PROBE_PATH = 'experimental/graph-explorer';

/**
 * True only when the server answers the probe with `enabled: true`. Any other
 * answer (`enabled: false` with the flag off, an error, a network failure) means off.
 */
export function explorerEnabledFrom(result) {
  return Boolean(result && result.success && result.enabled === true);
}

export async function fetchExplorerEnabled(apiManager) {
  try {
    const results = await apiManager.execute([
      { id: 'explorer-flag', path: EXPLORER_PROBE_PATH, method: 'GET', params: {} },
    ]);
    return explorerEnabledFrom(results['explorer-flag']);
  } catch (e) {
    return false;
  }
}
