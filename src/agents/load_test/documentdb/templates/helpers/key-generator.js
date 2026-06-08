// Random key generation matching the seeder's deterministic format.
//
// The seeder generates `primary_id` values like `doc-000001`, `doc-000002`, ...
// using zero-padded indices. This helper reconstructs valid keys at test time
// so generated queries hit existing documents.

const PAD_WIDTH = 6;

// Generate a key that matches a seeded document.
//
// Used by read/update/delete operations that need to target existing data.
export function generateExistingKey(keyCount) {
  const idx = Math.floor(Math.random() * keyCount);
  return `doc-${String(idx).padStart(PAD_WIDTH, '0')}`;
}

// Generate a key that does NOT match any seeded document.
//
// Used by insert operations to avoid duplicate-key errors. Range starts above
// the seeded key range and uses a wide random window so concurrent inserts
// don't collide.
export function generateNewKey(seededKeyCount) {
  const offset = seededKeyCount + Math.floor(Math.random() * 10_000_000);
  return `doc-${String(offset).padStart(PAD_WIDTH, '0')}`;
}

// Generate a synthetic document with a fresh primary_id.
//
// Used by insertOne/insertMany scenarios. Field values are deterministic
// per index so write throughput isn't dominated by data-generation cost.
export function generateNewDocument(seededKeyCount) {
  const idx = seededKeyCount + Math.floor(Math.random() * 10_000_000);
  const padded = String(idx).padStart(PAD_WIDTH, '0');
  return {
    primary_id: `doc-${padded}`,
    payload: `value-${padded}`,
    inserted_at: new Date().toISOString(),
  };
}
