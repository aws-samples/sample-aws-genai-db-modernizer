// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2310: Cloudscape components do not forward a `style` prop to the DOM, so
 * it is silently lost. This walks every UI source file and fails when a
 * component imported from @cloudscape-design gets a `style` attribute. Put the
 * style on a plain element, or use a className (see components/StyledBlocks.js).
 * A component whose typings declare a `style` API (Badge, Button, ...) may get
 * a `style` only in that API's shape, an object whose keys are the API's
 * top-level keys. Today that is `{ root: ... }`; a flat CSS object is still
 * dropped there, so it is refused. A value the checker cannot read (a call or
 * an identifier) is accepted for those components.
 * Caught: JSX attributes, spreads of an object literal that holds `style`,
 * `const B = Box` aliases, namespace imports (`<CS.Box style>`) and
 * `React.createElement(Box, { style })`.
 */
const fs = require('fs');
const path = require('path');
const { parse } = require('@babel/parser');

const SRC = path.join(__dirname, '..');

// Every .js file under src/ except tests (readdir's recursive mode, Node 20+).
function sourceFiles() {
  return fs.readdirSync(SRC, { recursive: true })
    .filter((rel) => /\.jsx?$/.test(rel) && !rel.split(path.sep).includes('__tests__'))
    .map((rel) => ({ rel, file: `${SRC}/${rel}` }));
}

const COMPONENTS = path.join(__dirname, '..', '..', 'node_modules', '@cloudscape-design', 'components');

const STYLE_API_KEYS = new Set(['root']);

// True when the component's typings declare a `style` prop (Cloudscape's style API).
function hasStyleApi(source) {
  const m = /^@cloudscape-design\/components\/([a-z-]+)$/.exec(source);
  if (!m) return false;
  const file = `${COMPONENTS}/${m[1]}/interfaces.d.ts`;
  return fs.existsSync(file) && /^\s+style\?:/m.test(fs.readFileSync(file, 'utf8'));
}

function walk(node, visit) {
  if (!node || typeof node.type !== 'string') return;
  visit(node);
  for (const key of Object.keys(node)) {
    const v = node[key];
    if (Array.isArray(v)) v.forEach((c) => c && walk(c, visit));
    else if (v && typeof v === 'object') walk(v, visit);
  }
}

const keyName = (p) => (p.key && (p.key.name || p.key.value)) || null;
const objectHasStyle = (o) => o.type === 'ObjectExpression' && o.properties.some((p) => p.type === 'ObjectProperty' && keyName(p) === 'style');

// A `style` value allowed on a component with a style API: not a literal, or a literal of API keys only.
function styleValueOk(value) {
  if (!value) return false;
  const v = value.type === 'JSXExpressionContainer' ? value.expression : value;
  if (v.type !== 'ObjectExpression') return v.type !== 'StringLiteral' && v.type !== 'JSXEmptyExpression';
  return v.properties.every((p) => p.type === 'ObjectProperty' && STYLE_API_KEYS.has(keyName(p)));
}

function offenders(code) {
  const ast = parse(code, { sourceType: 'module', plugins: ['jsx'] });
  const bindings = new Map(); // local name -> { styleApi }
  const namespaces = new Set();
  walk(ast.program, (n) => {
    if (n.type !== 'ImportDeclaration' || !n.source.value.startsWith('@cloudscape-design/')) return;
    const styleApi = hasStyleApi(n.source.value);
    n.specifiers.forEach((sp) => {
      if (sp.type === 'ImportNamespaceSpecifier') namespaces.add(sp.local.name);
      else bindings.set(sp.local.name, { styleApi });
    });
  });
  // Aliases: const B = Box (repeat so a chain of aliases resolves).
  for (let i = 0; i < 3; i += 1) {
    walk(ast.program, (n) => {
      if (n.type === 'VariableDeclarator' && n.id.type === 'Identifier' && n.init && n.init.type === 'Identifier' && bindings.has(n.init.name)) {
        bindings.set(n.id.name, bindings.get(n.init.name));
      }
    });
  }
  const resolve = (nameNode) => {
    if (!nameNode) return null;
    if (nameNode.type === 'Identifier' || nameNode.type === 'JSXIdentifier') return bindings.get(nameNode.name) ? { label: nameNode.name, ...bindings.get(nameNode.name) } : null;
    const obj = nameNode.object;
    if (obj && (obj.type === 'JSXIdentifier' || obj.type === 'Identifier') && namespaces.has(obj.name)) {
      return { label: `${obj.name}.${(nameNode.property || {}).name}`, styleApi: false };
    }
    return null;
  };
  const found = [];
  walk(ast.program, (n) => {
    if (n.type === 'JSXOpeningElement') {
      const c = resolve(n.name);
      if (!c) return;
      for (const a of n.attributes) {
        if (a.type === 'JSXAttribute' && a.name.name === 'style') {
          if (!c.styleApi || !styleValueOk(a.value)) found.push(`${c.label} at line ${n.loc.start.line}`);
        } else if (a.type === 'JSXSpreadAttribute' && objectHasStyle(a.argument)) {
          found.push(`${c.label} at line ${n.loc.start.line} (spread)`);
        }
      }
    } else if (n.type === 'CallExpression') {
      const callee = n.callee;
      const isCreate = (callee.type === 'Identifier' && callee.name === 'createElement')
        || (callee.type === 'MemberExpression' && callee.property.name === 'createElement');
      if (!isCreate || n.arguments.length < 2) return;
      const c = resolve(n.arguments[0]);
      const props = n.arguments[1];
      if (!c || props.type !== 'ObjectExpression') return;
      props.properties.forEach((p) => {
        if (p.type === 'ObjectProperty' && keyName(p) === 'style' && (!c.styleApi || !styleValueOk(p.value))) found.push(`${c.label} at line ${n.loc.start.line} (createElement)`);
        if (p.type === 'SpreadElement' && objectHasStyle(p.argument)) found.push(`${c.label} at line ${n.loc.start.line} (createElement spread)`);
      });
    }
  });
  return found;
}

const BOX = "import Box from '@cloudscape-design/components/box';\n";
const BADGE = "import Badge from '@cloudscape-design/components/badge';\n";
const BUTTON = "import Button from '@cloudscape-design/components/button';\n";

test('the checker flags a style prop on a Cloudscape component', () => {
  const code = `${BOX}const a = <Box style={{ color: 'red' }}>x</Box>;\nconst b = <div style={{}}>y</div>;`;
  expect(offenders(code)).toEqual(['Box at line 2']);
});

test('the checker flags a namespace import', () => {
  const code = "import * as CS from '@cloudscape-design/components';\nconst a = <CS.Box style={{ color: 'red' }}>x</CS.Box>;";
  expect(offenders(code)).toEqual(['CS.Box at line 2']);
});

test('the checker flags a spread that carries style', () => {
  expect(offenders(`${BOX}const a = <Box {...{ style: { color: 'red' } }}>x</Box>;`)).toEqual(['Box at line 2 (spread)']);
  expect(offenders(`${BOX}const a = <Box {...props}>x</Box>;`)).toEqual([]);
});

test('the checker flags an alias of a Cloudscape component', () => {
  expect(offenders(`${BOX}const B = Box;\nconst C = B;\nconst a = <C style={{ color: 'red' }}>x</C>;`)).toEqual(['C at line 4']);
});

test('the checker flags createElement with a style', () => {
  expect(offenders(`import React from 'react';\n${BOX}const a = React.createElement(Box, { style: { color: 'red' } }, 'x');`)).toEqual(['Box at line 3 (createElement)']);
  expect(offenders(`import { createElement } from 'react';\n${BOX}const a = createElement(Box, { ...{ style: {} } }, 'x');`)).toEqual(['Box at line 3 (createElement spread)']);
  expect(offenders(`import React from 'react';\n${BOX}const a = React.createElement(Box, { padding: 's' }, 'x');`)).toEqual([]);
});

test('a component with a style API may use its { root } shape', () => {
  expect(offenders(`${BADGE}const a = <Badge style={{ root: { background: 'red' } }}>x</Badge>;`)).toEqual([]);
  expect(offenders(`${BADGE}const a = <Badge style={engineBadgeStyle(e)}>x</Badge>;`)).toEqual([]);
});

test('a flat CSS object is refused even on a component with a style API', () => {
  expect(offenders(`${BADGE}const a = <Badge style={{ background: 'red' }}>x</Badge>;`)).toEqual(['Badge at line 2']);
  expect(offenders(`${BUTTON}const a = <Button style={{ color: 'red' }}>x</Button>;`)).toEqual(['Button at line 2']);
});

test('no Cloudscape component in the UI source gets a dropped style prop', () => {
  const bad = [];
  expect(sourceFiles().length).toBeGreaterThan(50);
  for (const { rel, file } of sourceFiles()) {
    offenders(fs.readFileSync(file, 'utf8')).forEach((f) => bad.push(`${rel}: ${f}`));
  }
  expect(bad).toEqual([]);
});
