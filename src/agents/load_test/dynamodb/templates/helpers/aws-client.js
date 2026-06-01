import crypto from 'k6/crypto';
import http from 'k6/http';

const region = __ENV.AWS_REGION || 'us-east-1';
const service = 'dynamodb';
const host = `dynamodb.${region}.amazonaws.com`;
const endpoint = `https://${host}`;

function getSignatureKey(key, dateStamp, regionName, serviceName) {
  const kDate = crypto.hmac('sha256', 'AWS4' + key, dateStamp, 'binary');
  const kRegion = crypto.hmac('sha256', kDate, regionName, 'binary');
  const kService = crypto.hmac('sha256', kRegion, serviceName, 'binary');
  return crypto.hmac('sha256', kService, 'aws4_request', 'binary');
}

export function dynamoRequest(action, payload) {
  const now = new Date();
  const amzDate = now.toISOString().replace(/[:-]|\.\d{3}/g, '');
  const dateStamp = amzDate.slice(0, 8);
  const body = JSON.stringify(payload);

  const headers = {
    'Content-Type': 'application/x-amz-json-1.0',
    'X-Amz-Target': `DynamoDB_20120810.${action}`,
    'X-Amz-Date': amzDate,
    'Host': host,
  };

  if (__ENV.AWS_SESSION_TOKEN) {
    headers['X-Amz-Security-Token'] = __ENV.AWS_SESSION_TOKEN;
  }

  const signedHeaders = 'content-type;host;x-amz-date;x-amz-target';
  const canonicalHeaders = `content-type:${headers['Content-Type']}\nhost:${host}\nx-amz-date:${amzDate}\nx-amz-target:${headers['X-Amz-Target']}\n`;
  const payloadHash = crypto.sha256(body, 'hex');
  const canonicalRequest = `POST\n/\n\n${canonicalHeaders}\n${signedHeaders}\n${payloadHash}`;
  const credentialScope = `${dateStamp}/${region}/${service}/aws4_request`;
  const stringToSign = `AWS4-HMAC-SHA256\n${amzDate}\n${credentialScope}\n${crypto.sha256(canonicalRequest, 'hex')}`;
  const signingKey = getSignatureKey(__ENV.AWS_SECRET_ACCESS_KEY, dateStamp, region, service);
  const signature = crypto.hmac('sha256', signingKey, stringToSign, 'hex');

  headers['Authorization'] = `AWS4-HMAC-SHA256 Credential=${__ENV.AWS_ACCESS_KEY_ID}/${credentialScope}, SignedHeaders=${signedHeaders}, Signature=${signature}`; // nosemgrep: no-stringify-keys

  return http.post(endpoint, body, { headers });
}
