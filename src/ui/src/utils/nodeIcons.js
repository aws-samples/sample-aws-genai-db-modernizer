// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * The graph explorer's one icon map (#308): node type, or engine id from the
 * engine catalog, to its icon. Identity is never the icon alone: every use
 * shows the label beside it (the engine's catalog display name). A new node
 * type or engine is one entry here.
 *
 * Engines use the official AWS Architecture Icons (48 px service icons,
 * assets/aws-icons, unmodified); the other node types use Lucide icons
 * (assets/lucide-icons, ISC). See the README in each directory.
 */

import amazonAurora from '../assets/aws-icons/amazon-aurora.svg';
import amazonDocumentdb from '../assets/aws-icons/amazon-documentdb.svg';
import amazonDynamodb from '../assets/aws-icons/amazon-dynamodb.svg';
import amazonElasticache from '../assets/aws-icons/amazon-elasticache.svg';
import amazonKeyspaces from '../assets/aws-icons/amazon-keyspaces.svg';
import amazonNeptune from '../assets/aws-icons/amazon-neptune.svg';
import amazonOpensearch from '../assets/aws-icons/amazon-opensearch-service.svg';
import database from '../assets/lucide-icons/database.svg';
import fileCode from '../assets/lucide-icons/file-code.svg';
import squareFunction from '../assets/lucide-icons/square-function.svg';
import table from '../assets/lucide-icons/table.svg';
import tableMuted from '../assets/lucide-icons/table-muted.svg';
import zap from '../assets/lucide-icons/zap.svg';
import { displayEngine } from './engineNames';

// Engine id (src/catalog/engines) -> its AWS service icon. Aurora MySQL and
// Aurora PostgreSQL share the one Aurora service icon; their labels differ.
export const ENGINE_ICONS = Object.freeze({
  aurora: amazonAurora,
  aurora_mysql: amazonAurora,
  aurora_postgresql: amazonAurora,
  dynamodb: amazonDynamodb,
  documentdb: amazonDocumentdb,
  elasticache: amazonElasticache,
  opensearch: amazonOpensearch,
  keyspaces: amazonKeyspaces,
  neptune: amazonNeptune,
});

// An engine with no icon of its own: a generic database, with its label.
export const FALLBACK_ENGINE_ICON = database;

// Node type -> icon and the i18n key of its name. Stored procedures, triggers
// and code modules have no nodes yet; their entries are reserved so the work
// that adds them only plugs in data.
export const NODE_TYPE_ICONS = Object.freeze({
  source_database: { icon: database, labelKey: 'graph-explorer.icons.source-database' },
  table: { icon: table, labelKey: 'graph-explorer.icons.table' },
  placeholder_table: { icon: tableMuted, labelKey: 'graph-explorer.node.no-table' },
  stored_procedure: { icon: squareFunction, labelKey: 'graph-explorer.icons.stored-procedure' },
  trigger: { icon: zap, labelKey: 'graph-explorer.icons.trigger' },
  code_module: { icon: fileCode, labelKey: 'graph-explorer.icons.code-module' },
});

/** An engine's icon and label; an engine without its own icon falls back. */
export function engineIcon(engineId) {
  const own = ENGINE_ICONS[engineId];
  return { src: own || FALLBACK_ENGINE_ICON, label: displayEngine(engineId), fallback: !own };
}

/** A graph node's icon: its engine's, or its node type's. */
export function nodeIcon(node) {
  if (node.kind === 'engine') return engineIcon(node.id).src;
  if (node.kind === 'table') return (node.placeholder ? NODE_TYPE_ICONS.placeholder_table : NODE_TYPE_ICONS.table).icon;
  const type = NODE_TYPE_ICONS[node.kind];
  return type ? type.icon : FALLBACK_ENGINE_ICON;
}
