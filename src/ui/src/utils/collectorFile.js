// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * What the new-assessment form can fill in from a chosen collector file (#2327): the
 * database name and the source engine the file names. The file is untrusted data: only
 * these two strings are read, the form shows them as text, and the user can edit both.
 * The server still reads the file itself; this is only a starting value for the form.
 */

import { isValidDatabaseName } from './databaseName';

// Larger files are not read in the browser; the form then starts empty.
export const MAX_PREFILL_BYTES = 50 * 1024 * 1024;

// The source engines the form offers, besides "Detect from file".
export const SOURCE_ENGINE_IDS = ['mysql', 'postgresql', 'mariadb', 'sqlserver', 'oracle'];

const ENGINE_ALIASES = { postgres: 'postgresql' };
const KNOWN_ENGINES = ['mysql', 'mariadb', 'postgresql', 'sqlserver', 'oracle', 'db2'];
// The version banners the server recognises, in its order.
const VERSION_MARKERS = [
  ['postgres', 'postgresql'],
  ['sql server', 'sqlserver'],
  ['oracle', 'oracle'],
  ['db2', 'db2'],
  ['mariadb', 'mariadb'],
];

/**
 * The engine a collection's metadata names, or null. A copy of
 * `recognised_source_engine` in src/tools/database/offline_parser.py: the explicit
 * `engine` first, then the version banner; a plain MySQL version number names none.
 * The shared fixture fixtures/sourceEngineFromFile.json keeps the two in step.
 */
export function sourceEngineFromMetadata(metadata) {
  const meta = metadata && typeof metadata === 'object' ? metadata : {};
  let engine = String(meta.engine || '').trim().toLowerCase();
  engine = ENGINE_ALIASES[engine] || engine;
  if (KNOWN_ENGINES.includes(engine)) {
    return engine;
  }
  const version = String(meta.version || meta.version_full || meta.product_version || '').toLowerCase();
  const hit = VERSION_MARKERS.find(([marker]) => version.includes(marker));
  return hit ? hit[1] : null;
}

function parseCollection(text) {
  const body = String(text || '').replace(/^\uFEFF/, '');
  const start = body.indexOf('{');
  if (start === -1) {
    return null;
  }
  // A script may print one more object after the collection (its addenda).
  const addendum = body.slice(start + 1).search(/\{\s*"lob_sizes"/);
  const candidates = [body.slice(start, body.lastIndexOf('}') + 1)];
  if (addendum !== -1) {
    const cut = body.slice(start, start + 1 + addendum);
    candidates.push(cut.slice(0, cut.lastIndexOf('}') + 1));
  }
  for (const candidate of candidates) {
    try {
      const parsed = JSON.parse(candidate);
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        return parsed;
      }
    } catch {
      // try the next candidate
    }
  }
  return null;
}

const text = (value) => (typeof value === 'string' && value.trim() ? value.trim() : null);

/**
 * `{ databaseName, sourceEngine }` from a collector file's text, each null when the file
 * does not name it (or is not JSON). Reads a raw collection (`metadata.database_name`)
 * and a collector output (`metadata.source_database`).
 */
export function collectorFileFacts(fileText) {
  const collection = parseCollection(fileText);
  const metadata = collection && typeof collection.metadata === 'object' ? collection.metadata : null;
  if (!metadata) {
    return { databaseName: null, sourceEngine: null };
  }
  const source = metadata.source_database && typeof metadata.source_database === 'object'
    ? metadata.source_database
    : null;
  // Only a name the form would accept (#2327 review): a long or odd one is left for the user.
  const name = text(source ? source.database_name : metadata.database_name);
  const engine = sourceEngineFromMetadata(source || metadata);
  return {
    databaseName: name && isValidDatabaseName(name) ? name : null,
    sourceEngine: KNOWN_ENGINES.includes(engine) ? engine : null,
  };
}

function fileText(file) {
  if (typeof file.text === 'function') {
    return file.text();
  }
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ''));
    reader.onerror = () => reject(reader.error);
    reader.readAsText(file);
  });
}

/** The facts of a chosen File, or both null for a file too large to read here. */
export async function readCollectorFile(file) {
  if (!file || file.size > MAX_PREFILL_BYTES) {
    return { databaseName: null, sourceEngine: null };
  }
  try {
    return collectorFileFacts(await fileText(file));
  } catch {
    return { databaseName: null, sourceEngine: null };
  }
}
