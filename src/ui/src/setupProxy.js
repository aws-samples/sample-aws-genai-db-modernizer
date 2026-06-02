/**
 * Development proxy — forwards /api/* to the deployed ALB.
 *
 * Set the target via environment variable:
 *   REACT_APP_API_PROXY=https://modern-Appli-xxx.us-east-1.elb.amazonaws.com npm start
 *
 * Falls back to the feature branch ALB if not set.
 */
const { createProxyMiddleware } = require('http-proxy-middleware');

module.exports = function(app) {
  const target = process.env.REACT_APP_API_PROXY;

  if (!target) {
    console.warn('[proxy] REACT_APP_API_PROXY not set. Copy .env.example to .env.development');
    return;
  }

  console.info(`[proxy] Forwarding /api/* → ${target}`);

  app.use(
    '/api',
    createProxyMiddleware({
      target,
      changeOrigin: true,
      secure: false, // ALB uses self-signed cert internally
    })
  );
};
