/**
 * The engine catalog's generated tables (#492) replace hand-kept copies without
 * changing a single value. Each literal below is the table as it stood on main before
 * the catalog, kept verbatim, so a catalog edit that changes what a page shows fails
 * here and has to be made on purpose.
 */
import {
  ENGINE_HEX, ENGINE_LABELS, ENGINE_PICKER_ORDER, CACHE_ENGINE_IDS,
} from '../engineCatalog.generated';
import * as generated from '../engineCatalog.generated';
import { ENGINE_LABELS as NAMES_LABELS } from '../engineNames';
import { CACHE_ENGINES } from '../cacheLayer';
import { NODE_COLORS } from '../../components/ChartSankey';
import { ENGINE_STYLES } from '../engineColors';

const ENGINE_LABELS_BEFORE = {
  dynamodb: 'DynamoDB',
  documentdb: 'DocumentDB',
  opensearch: 'OpenSearch',
  elasticache: 'ElastiCache',
  aurora_postgresql: 'Aurora PostgreSQL',
  aurora_mysql: 'Aurora MySQL',
  neptune: 'Neptune',
  keyspaces: 'Keyspaces',
  aurora: 'Aurora',
};

const NODE_COLORS_BEFORE = {
  patterns: '#5f6b7a',
  dynamodb: '#3184e8',
  documentdb: '#1d8102',
  elasticache: '#d13212',
  opensearch: '#2ea597',
  neptune: '#7d2105',
  keyspaces: '#8b6ccb',
  aurora_mysql: '#9c5700',
  // #1246: Aurora PostgreSQL gets its own colour, so the two Aurora engines are told
  // apart in every view and in the executive report's share bar.
  aurora_postgresql: '#c33d69',
  aurora: '#ec7211',
};

// #1246 retired the named badge colours (colors.ui_badge): every engine badge is now
// filled with the engine's one colour, so the gate's styles carry no `badge` any more.
const ASSIGNMENT_GATE_COLORS_BEFORE = {
  dynamodb: { bg: '#3184e8', label: 'DynamoDB' },
  documentdb: { bg: '#1d8102', label: 'DocumentDB' },
  opensearch: { bg: '#2ea597', label: 'OpenSearch' },
  elasticache: { bg: '#d13212', label: 'ElastiCache' },
  neptune: { bg: '#7d2105', label: 'Neptune' },
  keyspaces: { bg: '#8b6ccb', label: 'Keyspaces' },
  aurora: { bg: '#ec7211', label: 'Aurora' },
  aurora_mysql: { bg: '#9c5700', label: 'Aurora MySQL' },
  aurora_postgresql: { bg: '#c33d69', label: 'Aurora PostgreSQL' },
};

test('display names keep their values and order', () => {
  expect(NAMES_LABELS).toBe(ENGINE_LABELS);
  expect(Object.entries(ENGINE_LABELS)).toEqual(Object.entries(ENGINE_LABELS_BEFORE));
});

test('the cache-engine set is unchanged', () => {
  expect([...CACHE_ENGINES]).toEqual(['elasticache']);
  expect(CACHE_ENGINE_IDS).toEqual(['elasticache']);
});

test('Sankey node colours are unchanged for every node id', () => {
  expect(NODE_COLORS).toEqual(NODE_COLORS_BEFORE);
});

test('assignment gate colours, labels and picker order are unchanged', () => {
  // Key order matters: AssignmentGate.js builds its engine picker from it.
  expect(Object.entries(ENGINE_STYLES)).toEqual(Object.entries(ASSIGNMENT_GATE_COLORS_BEFORE));
});

test('every engine has a colour, and the catalog exports no second palette', () => {
  Object.keys(ENGINE_LABELS).forEach((engine) => {
    expect(ENGINE_HEX[engine]).toMatch(/^#[0-9a-f]{6}$/i);
  });
  // #1246: one colour per engine; the named badge palette is gone.
  expect(generated.ENGINE_BADGE_COLORS).toBeUndefined();
});

test('every catalog engine is in the assignment gate picker, once', () => {
  // ENGINE_PICKER_ORDER is generated from every engine file, so a new catalog engine
  // is in ENGINE_LABELS and here alike, and gets a colour and label in the gate.
  expect([...ENGINE_PICKER_ORDER].sort()).toEqual(Object.keys(ENGINE_LABELS).sort());
  expect(Object.keys(ENGINE_STYLES)).toEqual(ENGINE_PICKER_ORDER);
  Object.values(ENGINE_STYLES).forEach((style) => {
    expect(style.bg).toBeDefined();
    expect(style.label).toBeDefined();
  });
});
