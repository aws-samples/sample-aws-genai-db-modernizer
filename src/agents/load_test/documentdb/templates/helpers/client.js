// xk6-mongo client setup for DocumentDB.
//
// Connection uses MONGODB-AWS authentication: the xk6-mongo extension is built
// on the official Go MongoDB driver, which auto-resolves credentials from the
// ECS task role via the AWS SDK chain. No credentials are embedded in the URI.
//
// If a future xk6-mongo version doesn't pass MONGODB-AWS auth options through
// the connection string, the fallback is to set ACCESS_KEY and SECRET_KEY env
// vars and embed them in the URI as a userinfo prefix (still IAM-based, just
// key-based instead of role-based credential resolution).
//
// Required env vars:
//   DOCDB_ENDPOINT       — cluster endpoint hostname
//   DOCDB_REPLICA_COUNT  — for read preference selection (defaults to "0")

import mongo from 'k6/x/mongo';

const ENDPOINT = __ENV.DOCDB_ENDPOINT;
const REPLICA_COUNT = parseInt(__ENV.DOCDB_REPLICA_COUNT || '0', 10);
const DB_NAME = 'loadtest';
const CA_BUNDLE = '/etc/ssl/certs/docdb-global-bundle.pem';

const READ_PREFERENCE = REPLICA_COUNT > 0 ? 'secondaryPreferred' : 'primary';

const URI =
  `mongodb://${ENDPOINT}:27017/${DB_NAME}` +
  `?tls=true` +
  `&tlsCAFile=${encodeURIComponent(CA_BUNDLE)}` +
  `&replicaSet=rs0` +
  `&authSource=%24external` +
  `&authMechanism=MONGODB-AWS` +
  `&readPreference=${READ_PREFERENCE}` +
  `&retryWrites=false`;

const client = mongo.newClient(URI);
const db = client.database(DB_NAME);

export function getCollection(collectionName) {
  return db.collection(collectionName);
}

// Operation dispatcher — one place to swap xk6-mongo API quirks if needed.
export function runOperation(collectionName, operation, args) {
  const collection = getCollection(collectionName);

  switch (operation) {
    case 'findOne':
      return collection.findOne(args.filter || {});
    case 'find':
      return collection.find(args.filter || {}, args.options || {});
    case 'aggregate':
      return collection.aggregate(args.pipeline || []);
    case 'insertOne':
      return collection.insertOne(args.document);
    case 'insertMany':
      return collection.insertMany(args.documents);
    case 'updateOne':
      return collection.updateOne(args.filter, args.update);
    case 'updateMany':
      return collection.updateMany(args.filter, args.update);
    case 'deleteOne':
      return collection.deleteOne(args.filter);
    case 'deleteMany':
      return collection.deleteMany(args.filter);
    case 'bulkWrite':
      return collection.bulkWrite(args.operations);
    default:
      throw new Error(`Unsupported operation: ${operation}`);
  }
}
