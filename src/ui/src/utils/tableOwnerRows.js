// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { tableOwnershipOf, tableSourcesOf } from './reportFacts';

/**
 * #2321: the rows of the results page's "Table owners" table. Each row joins what
 * report.json's facts state about one table: its owner and the engines that read a CDC
 * copy of it (facts.table_owner_lines), its cache, and the reads a replica serves
 * (facts.table_sources, #992). It only joins those facts by table name (ADR-032): no
 * count is made here.
 *
 * A report with no table_owner_lines (a job whose schema design did not run) still
 * states each written table's source of truth and replicas, so its rows come from
 * facts.table_sources alone: the source of truth is the owner, the replicas are the
 * CDC targets, and nothing is cached.
 */

function sharesRisk(row) {
  return row && row.shares_replica_risk === true;
}

function replicaReads(source) {
  return source && typeof source.replica_read_count === 'number' ? source.replica_read_count : null;
}

// A replicated table whose facts say it does not share the standard risk sentence (a
// report written with older wording) keeps its own stored risk.
function ownRiskOf(source) {
  return source && !sharesRisk(source) && source.risk ? source.risk : null;
}

export function tableOwnerRows(synthesis) {
  const ownership = tableOwnershipOf(synthesis);
  const sources = tableSourcesOf(synthesis);
  const replicated = (sources && sources.replicated_tables) || [];
  const byTable = new Map(replicated.map(row => [row.table, row]));
  if (ownership) {
    return (ownership.table_lines || []).map((line) => {
      const source = byTable.get(line.table);
      return {
        table: line.table,
        owner: line.owner || null,
        split: line.owner_basis === 'split_writes',
        cdcTo: line.also_read_by || [],
        replicaReads: replicaReads(source),
        cachedBy: line.cached_by || null,
        ownRisk: ownRiskOf(source),
      };
    });
  }
  if (!sources) return [];
  // The facts name a single-engine table but not its source of truth, so those tables
  // are stated by the callout's own line, not as rows.
  return replicated.map(source => ({
    table: source.table,
    owner: source.source_of_truth,
    split: false,
    cdcTo: source.replica_engines || [],
    replicaReads: replicaReads(source),
    cachedBy: null,
    ownRisk: ownRiskOf(source),
  }));
}
