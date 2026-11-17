// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2327: the new-assessment form fills in the database name and the source engine from
 * the chosen file. The engine rule is a copy of the server's detector; the shared
 * fixture (also read by tests/unit/tools/database/test_ui_source_engine_parity.py)
 * keeps them in step.
 */
import fixture from './fixtures/sourceEngineFromFile.json';
import {
  MAX_PREFILL_BYTES, collectorFileFacts, readCollectorFile, sourceEngineFromMetadata,
} from '../collectorFile';

test.each(fixture.cases.map((c) => [JSON.stringify(c.metadata), c]))(
  'the engine named by %s matches the server detector',
  (_, { metadata, engine }) => {
    expect(sourceEngineFromMetadata(metadata)).toBe(engine);
  },
);

test('a raw collection with a header line gives its database name and engine', () => {
  const text = 'collection_output\n{"tables": [], "queries": [], "metadata": {"database_name": "trailgear", "version": "PostgreSQL 16.4"}}\n(1 row)';
  expect(collectorFileFacts(text)).toEqual({ databaseName: 'trailgear', sourceEngine: 'postgresql' });
});

test('a collector output gives the source database it names', () => {
  const text = JSON.stringify({ metadata: { source_database: { engine: 'mariadb', database_name: ' shop ' } } });
  expect(collectorFileFacts(text)).toEqual({ databaseName: 'shop', sourceEngine: 'mariadb' });
});

test('a collection followed by its addenda object still reads', () => {
  const text = '\uFEFF{"metadata": {"database_name": "wp", "version": "8.0.36"}}\n{"lob_sizes": []}';
  expect(collectorFileFacts(text)).toEqual({ databaseName: 'wp', sourceEngine: null });
});

test.each([
  ['not JSON', 'hello'],
  ['no metadata', '{"tables": []}'],
  ['an array', '[1, 2]'],
  ['a blank name', '{"metadata": {"database_name": "  "}}'],
  ['a non-text name', '{"metadata": {"database_name": 42}}'],
  ['a name the form would refuse', '{"metadata": {"database_name": "my db!"}}'],
  ['a name over 128 characters', JSON.stringify({ metadata: { database_name: 'a'.repeat(129) } })],
  ['an engine the server does not know', '{"metadata": {"engine": "sybase"}}'],
])('a file with %s names nothing', (_, text) => {
  expect(collectorFileFacts(text)).toEqual({ databaseName: null, sourceEngine: null });
});

test('a file above the size cap is not read', async () => {
  const file = { size: MAX_PREFILL_BYTES + 1, text: jest.fn() };
  expect(await readCollectorFile(file)).toEqual({ databaseName: null, sourceEngine: null });
  expect(file.text).not.toHaveBeenCalled();
});

test('a file that cannot be read names nothing', async () => {
  const file = { size: 10, text: () => Promise.reject(new Error('gone')) };
  expect(await readCollectorFile(file)).toEqual({ databaseName: null, sourceEngine: null });
});

test('a 5,000,000-character name is dropped, the engine kept', () => {
  const text = JSON.stringify({ metadata: { database_name: 'a'.repeat(5000000), engine: 'oracle' } });
  expect(collectorFileFacts(text)).toEqual({ databaseName: null, sourceEngine: 'oracle' });
});
