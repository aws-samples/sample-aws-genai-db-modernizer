/**
 * The engine catalog's generated tables (#492) replace hand-kept copies without
 * changing a single value. Each literal below is the table as it stood on main before
 * the catalog, kept verbatim, so a catalog edit that changes what a page shows fails
 * here and has to be made on purpose.
 */
import {
  ENGINE_BADGE_COLORS, ENGINE_HEX, ENGINE_LABELS, ENGINE_PICKER_ORDER, CACHE_ENGINE_IDS,
} from '../engineCatalog.generated';
import { ENGINE_LABELS as NAMES_LABELS } from '../engineNames';
import { CACHE_ENGINES } from '../cacheLayer';
import { NODE_COLORS } from '../../components/ChartSankey-01';
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
  aurora_postgresql: '#9c5700',
  aurora: '#ec7211',
};

const ASSIGNMENT_GATE_COLORS_BEFORE = {
  dynamodb: { bg: '#3184e8', badge: 'blue', label: 'DynamoDB' },
  documentdb: { bg: '#1d8102', badge: 'green', label: 'DocumentDB' },
  opensearch: { bg: '#2ea597', badge: 'grey', label: 'OpenSearch' },
  elasticache: { bg: '#d13212', badge: 'red', label: 'ElastiCache' },
  neptune: { bg: '#7d2105', badge: 'red', label: 'Neptune' },
  keyspaces: { bg: '#8b6ccb', badge: 'blue', label: 'Keyspaces' },
  aurora: { bg: '#ec7211', badge: 'green', label: 'Aurora' },
  aurora_mysql: { bg: '#9c5700', badge: 'green', label: 'Aurora MySQL' },
  aurora_postgresql: { bg: '#9c5700', badge: 'green', label: 'Aurora PostgreSQL' },
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

test('assignment gate colours, badges, labels and picker order are unchanged', () => {
  // Key order matters: AssignmentGate.js builds its engine picker from it.
  expect(Object.entries(ENGINE_STYLES)).toEqual(Object.entries(ASSIGNMENT_GATE_COLORS_BEFORE));
});

test('every engine has a colour and a badge colour', () => {
  Object.keys(ENGINE_LABELS).forEach((engine) => {
    expect(ENGINE_HEX[engine]).toMatch(/^#[0-9a-f]{6}$/i);
    expect(['blue', 'grey', 'green', 'red']).toContain(ENGINE_BADGE_COLORS[engine]);
  });
});

test('every catalog engine is in the assignment gate picker, once', () => {
  // ENGINE_PICKER_ORDER is generated from every engine file, so a new catalog engine
  // is in ENGINE_LABELS and here alike, and gets a colour, badge and label in the gate.
  expect([...ENGINE_PICKER_ORDER].sort()).toEqual(Object.keys(ENGINE_LABELS).sort());
  expect(Object.keys(ENGINE_STYLES)).toEqual(ENGINE_PICKER_ORDER);
  Object.values(ENGINE_STYLES).forEach((style) => {
    expect(style.bg).toBeDefined();
    expect(style.badge).toBeDefined();
    expect(style.label).toBeDefined();
  });
});
