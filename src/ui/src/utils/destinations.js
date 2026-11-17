// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2320: where a pattern's destination comes from, in one place, so every row source (owner
 * engines and the cache layer) feeds the explorer's cells the same way.
 *
 * `destName` is the single label the filter, the by-destination view and the detail panel use.
 * `destNames` lists each destination on its own: one entry when the pattern names its
 * destination, otherwise (Aurora, whose destination is its source tables) one per source table.
 * `named` is true only when the pattern names a destination table of its own. An Aurora pattern
 * carries each source table over 1:1 and names none, so it is not a destination any table merges into.
 */
export const destinationOf = (ap, shorten = (t) => t) => {
  const named = ap?.table_name || ap?.key_pattern || ap?.index_or_stream || ap?.index || ap?.collection || null;
  const fromSources = (ap?.source_tables || []).map(shorten);
  return {
    destName: named || fromSources.join(', ') || '—',
    destNames: named ? [named] : fromSources,
    named: !!named,
  };
};

// One destination's identity: the same name on two engines is two destinations.
export const destinationKey = (engine, name) => `${engine}::${name}`;
