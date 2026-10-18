/**
 * The connection the Create Analysis form sends (#723, ADR-004 Decision 5).
 *
 * Database credentials are named by reference only: the ARN or the name of an
 * AWS Secrets Manager secret, which the collector reads through its IAM role.
 * A username or password is never sent -- the API refuses a request that
 * carries one (src/api/secret_guard.py) -- so this builds the connection from
 * an explicit list of fields and ignores any credential left in the form state.
 */
import { normalizeDatabaseName } from './databaseName';

export const CREDENTIAL_REFERENCE_TYPES = ['secrets-arn', 'secrets-name'];

export const NO_PASSWORD_GUIDANCE =
  'Passwords are never sent. Store the database credentials in AWS Secrets Manager '
  + '(or another secrets manager) and enter the secret ARN or name instead.';

export function buildConnection(formData) {
  const type = formData.credentialsType;
  if (!CREDENTIAL_REFERENCE_TYPES.includes(type)) {
    throw new Error(NO_PASSWORD_GUIDANCE);
  }
  const connection = {
    host: formData.host,
    port: parseInt(formData.port, 10),
    database: normalizeDatabaseName(formData.databaseName),
    credentials_type: type,
  };
  if (type === 'secrets-arn') {
    connection.secret_arn = formData.secretArn;
  } else {
    connection.secret_name = formData.secretName;
  }
  return connection;
}
