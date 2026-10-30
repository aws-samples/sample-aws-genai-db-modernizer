// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #723 (ADR-004 Decision 5): the Create Analysis form names the database
 * credentials by reference -- an AWS Secrets Manager ARN or name -- and never
 * sends a username or password. The API refuses a request that carries one
 * (src/api/secret_guard.py), so the form must never build one.
 */
import { buildConnection, CREDENTIAL_REFERENCE_TYPES } from '../connectionPayload';

const SECRET = 'Zq7-not-a-real-value-4Kx9'; // pragma: allowlist secret
const SECRET_NAME = 'prod/db/credentials'; // pragma: allowlist secret
const ARN = 'arn:aws:secretsmanager:us-east-1:123456789012:secret:prod/db/creds-AbCdEf';

// Form state as an older build of the page could hold it: every credential
// field filled in, whatever the selected type.
const formData = (credentialsType) => ({
  host: 'db.example.internal',
  port: '3306',
  databaseName: ' app ',
  credentialsType,
  secretArn: ARN,
  secretName: SECRET_NAME,
  username: 'app',
  password: SECRET,
});

test('only secret references are offered', () => {
  expect(CREDENTIAL_REFERENCE_TYPES).toEqual(['secrets-arn', 'secrets-name']);
});

test('a secret ARN is sent by reference', () => {
  expect(buildConnection(formData('secrets-arn'))).toEqual({
    host: 'db.example.internal',
    port: 3306,
    database: 'app',
    credentials_type: 'secrets-arn',
    secret_arn: ARN,
  });
});

test('a secret name is sent by reference', () => {
  expect(buildConnection(formData('secrets-name'))).toEqual({
    host: 'db.example.internal',
    port: 3306,
    database: 'app',
    credentials_type: 'secrets-name',
    secret_name: SECRET_NAME,
  });
});

test.each(CREDENTIAL_REFERENCE_TYPES)('%s never carries a username or password', (type) => {
  const sent = JSON.stringify(buildConnection(formData(type)));
  expect(sent).not.toMatch(/pass|pwd|user/i);
  expect(sent).not.toContain(SECRET);
});

test.each(['direct', 'password', '', undefined])(
  'any other credentials type (%p) is refused before anything is sent',
  (type) => {
    expect(() => buildConnection(formData(type))).toThrow(/Secrets Manager/);
  },
);

test('the refusal never repeats the password', () => {
  try {
    buildConnection(formData('direct'));
  } catch (error) {
    expect(error.message).not.toContain(SECRET);
    return;
  }
  throw new Error('expected buildConnection to throw');
});
