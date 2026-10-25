'use strict';

// Maps the webpack-dev-server 4 options in react-scripts 5.0.1's dev-server
// config to their webpack-dev-server 5 equivalents (#266).

function adaptDevServerConfig(createConfig) {
  return function adapt(proxy, allowedHost) {
    const { onBeforeSetupMiddleware, onAfterSetupMiddleware, https, ...config } =
      createConfig(proxy, allowedHost);

    // CRA passes [undefined] when bound to localhost (no LAN address), which
    // webpack-dev-server 5 rejects; 'auto' allows localhost and the bound host.
    if (
      Array.isArray(config.allowedHosts) &&
      !config.allowedHosts.every((h) => typeof h === 'string' && h)
    ) {
      config.allowedHosts = 'auto';
    }

    config.setupMiddlewares = (middlewares, devServer) => {
      // "Before" hooks run now, ahead of webpack-dev-server's own middlewares.
      if (onBeforeSetupMiddleware) {
        onBeforeSetupMiddleware(devServer);
      }
      // "After" hooks only register through devServer.app, so collect them into
      // an app-like recorder and append them after the built-in middlewares.
      if (onAfterSetupMiddleware) {
        onAfterSetupMiddleware({
          app: {
            use: (middleware) => middlewares.push({ name: 'cra-after', middleware }),
          },
        });
      }
      return middlewares;
    };

    if (https) {
      config.server = {
        type: 'https',
        options: https === true ? {} : https,
      };
    }
    return config;
  };
}

module.exports = adaptDevServerConfig;
