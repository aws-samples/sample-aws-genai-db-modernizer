'use strict';

// Dev server entry for `npm start` (#266).
//
// react-scripts 5.0.1 targets webpack-dev-server 4 and passes the options
// `onBeforeSetupMiddleware`, `onAfterSetupMiddleware` and `https`, which
// webpack-dev-server 5 removed. The package.json override keeps the patched
// webpack-dev-server 5 (security advisories), so this script wraps CRA's
// dev-server config and maps those options to `setupMiddlewares` / `server`
// before running CRA's own start script. Everything else is CRA unchanged.

// Bind to localhost unless the caller chose a host. CRA defaults to 0.0.0.0.
if (!process.env.HOST) {
  process.env.HOST = 'localhost';
}

const configPath = require.resolve('react-scripts/config/webpackDevServer.config');
const createConfig = require(configPath);

const adaptDevServerConfig = require('./adaptDevServerConfig');

require.cache[configPath].exports = adaptDevServerConfig(createConfig);

require('react-scripts/scripts/start');
