/**
 * @jest-environment node
 */
// `npm start` must hand webpack-dev-server 5 only options it accepts (#266).
const { validate } = require('schema-utils');
const schema = require('webpack-dev-server/lib/options.json');
const createConfig = require('react-scripts/config/webpackDevServer.config');
const adaptDevServerConfig = require('../../scripts/adaptDevServerConfig');

const check = (config) => validate(schema, config, { name: 'Dev Server' });

describe('dev server config', () => {
  it("is rejected by webpack-dev-server 5 as CRA ships it", () => {
    expect(() => check(createConfig(undefined, undefined))).toThrow(
      /onAfterSetupMiddleware|onBeforeSetupMiddleware/
    );
  });

  it('is accepted once adapted, with and without a proxy or LAN host', () => {
    const adapt = adaptDevServerConfig(createConfig);
    expect(() => check(adapt(undefined, undefined))).not.toThrow();
    expect(() => check(adapt(undefined, 'host.local'))).not.toThrow();
    const config = adapt(
      [{ context: ['/api'], target: 'http://localhost:8000' }],
      undefined
    );
    expect(() => check(config)).not.toThrow();
    expect(config.proxy).toHaveLength(1);
    expect(config.onAfterSetupMiddleware).toBeUndefined();
    expect(typeof config.setupMiddlewares).toBe('function');
  });

  it('keeps the CRA middlewares: before hooks run now, after hooks append', () => {
    const calls = [];
    const fake = () => ({
      onBeforeSetupMiddleware: () => calls.push('before'),
      onAfterSetupMiddleware: (ds) => ds.app.use(() => calls.push('after')),
    });
    const config = adaptDevServerConfig(fake)(undefined, undefined);
    const existing = [{ name: 'builtin', middleware: () => {} }];
    const result = config.setupMiddlewares(existing, {});
    expect(calls).toEqual(['before']);
    expect(result.map((m) => m.name)).toEqual(['builtin', 'cra-after']);
  });

  it('maps https to the server option', () => {
    const fake = () => ({ https: { key: 'k' }, allowedHosts: 'all' });
    const config = adaptDevServerConfig(fake)(undefined, undefined);
    expect(config.server).toEqual({ type: 'https', options: { key: 'k' } });
    expect(config.https).toBeUndefined();
  });
});
