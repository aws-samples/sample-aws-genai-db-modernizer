// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { tableOwnershipOf, tableSourcesOf } from './reportFacts';

/**
 * #2321: the rows of the results page's "Table owners" table. Each row states what
 * report.json's facts say about one table: its owner, the engines that read a CDC copy
 * of it, the reads those engines serve and its cache, all from its own line in
 * facts.table_owner_lines. The replica reads are the line's replica_read_count, which
 * the facts layer counts for every mapped table (0: none); null only when a report
 * saved before it cannot give it. Nothing is counted or joined here (ADR-032).
 *
 * A report with no table_owner_lines (a job whose schema design did not run) still
 * states each written table's source of truth and replicas, so its rows come from
 * facts.table_sources alone: the source of truth is the owner, the replicas are the
 * CDC targets, and nothing is cached.
 */

function countOf(row) {
  return row && typeof row.replica_read_count === 'number' ? row.replica_read_count : null;
}

export function tableOwnerRows(synthesis) {
  const ownership = tableOwnershipOf(synthesis);
  if (ownership) {
    return (ownership.table_lines || []).map(line => ({
      table: line.table,
      owner: line.owner || null,
      split: line.owner_basis === 'split_writes',
      cdcTo: line.also_read_by || [],
      replicaReads: countOf(line),
      cachedBy: line.cached_by || null,
    }));
  }
  const sources = tableSourcesOf(synthesis);
  if (!sources) return [];
  // The facts name a single-engine table but not its source of truth, so those tables
  // are stated by the callout's own line, not as rows.
  return (sources.replicated_tables || []).map(source => ({
    table: source.table,
    owner: source.source_of_truth,
    split: false,
    cdcTo: source.replica_engines || [],
    replicaReads: countOf(source),
    cachedBy: null,
  }));
}
