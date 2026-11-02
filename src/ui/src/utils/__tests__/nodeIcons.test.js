// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import { ENGINE_LABELS, ENGINE_PICKER_ORDER } from '../engineCatalog.generated';
import { ENGINE_ICONS, FALLBACK_ENGINE_ICON, NODE_TYPE_ICONS, engineIcon, nodeIcon } from '../nodeIcons';

test('every engine in the catalog has its own icon', () => {
  const catalog = new Set([...Object.keys(ENGINE_LABELS), ...ENGINE_PICKER_ORDER]);
  catalog.forEach((engine) => {
    expect([engine, Boolean(ENGINE_ICONS[engine])]).toEqual([engine, true]);
    expect(engineIcon(engine).fallback).toBe(false);
  });
});

test('engine labels come from the catalog, never the icon', () => {
  expect(engineIcon('aurora_mysql').label).toBe(ENGINE_LABELS.aurora_mysql);
  // One Aurora service icon, two engines told apart by their labels.
  expect(engineIcon('aurora_mysql').src).toBe(engineIcon('aurora_postgresql').src);
  expect(engineIcon('aurora_mysql').label).not.toBe(engineIcon('aurora_postgresql').label);
});

test('an unknown engine falls back to a generic database icon with its id as label', () => {
  expect(engineIcon('cassandra')).toEqual({ src: FALLBACK_ENGINE_ICON, label: 'cassandra', fallback: true });
});

test('every node type has an icon and a name, the reserved ones included', () => {
  ['source_database', 'table', 'placeholder_table', 'stored_procedure', 'trigger', 'code_module'].forEach((type) => {
    expect(NODE_TYPE_ICONS[type].icon).toBeTruthy();
    expect(NODE_TYPE_ICONS[type].labelKey).toMatch(/^graph-explorer\./);
  });
});

test('graph nodes get their icon', () => {
  expect(nodeIcon({ kind: 'engine', id: 'dynamodb' })).toBe(ENGINE_ICONS.dynamodb);
  expect(nodeIcon({ kind: 'table', id: 't' })).toBe(NODE_TYPE_ICONS.table.icon);
  expect(nodeIcon({ kind: 'table', id: 'unknown', placeholder: true })).toBe(NODE_TYPE_ICONS.placeholder_table.icon);
  expect(nodeIcon({ kind: 'trigger', id: 'x' })).toBe(NODE_TYPE_ICONS.trigger.icon);
  expect(nodeIcon({ kind: 'engine', id: 'nope' })).toBe(FALLBACK_ENGINE_ICON);
});
