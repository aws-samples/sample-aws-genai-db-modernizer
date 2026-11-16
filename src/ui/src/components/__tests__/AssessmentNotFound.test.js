// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2307: the not-found state is a Header, one sentence naming the id, and a
 * button back to the assessments list. Cloudscape ships ESM this repo's Jest
 * config cannot transform, so its components are plain DOM stand-ins.
 */
import fs from 'fs';
import path from 'path';
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import AssessmentNotFound, { AssessmentLookupPending } from '../AssessmentNotFound';
import en from '../../locales/en.json';

jest.mock('react-i18next', () => ({
  useTranslation: () => ({
    t: (key, opts) => {
      const text = jest.requireActual('../../locales/en.json')[key];
      return text === undefined ? key : text.replace('{{id}}', (opts && opts.id) || '');
    },
  }),
}));
const mockNavigate = jest.fn();
jest.mock('react-router-dom', () => ({ useNavigate: () => mockNavigate }));
jest.mock('../AppHeader', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/app-layout', () => ({ __esModule: true, default: ({ content, breadcrumbs }) => <div>{breadcrumbs}{content}</div> }));
jest.mock('@cloudscape-design/components/side-navigation', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/breadcrumb-group', () => ({
  __esModule: true,
  default: ({ items }) => <ol>{items.map((i) => <li key={i.text} data-href={i.href}>{i.text}</li>)}</ol>,
}));
jest.mock('@cloudscape-design/components/status-indicator', () => ({ __esModule: true, default: ({ type, children }) => <span data-type={type}>{children}</span> }));
jest.mock('@cloudscape-design/components/container', () => ({ __esModule: true, default: ({ header, children }) => <div>{header}{children}</div> }));
jest.mock('@cloudscape-design/components/header', () => ({ __esModule: true, default: ({ children }) => <h1>{children}</h1> }));
jest.mock('@cloudscape-design/components/space-between', () => ({ __esModule: true, default: ({ children }) => <div>{children}</div> }));
jest.mock('@cloudscape-design/components/box', () => ({ __esModule: true, default: ({ children }) => <p>{children}</p> }));
jest.mock('@cloudscape-design/components/button', () => ({
  __esModule: true,
  default: ({ children, href, onFollow }) => (
    <a href={href} onClick={(e) => onFollow && onFollow({ preventDefault: () => e.preventDefault(), detail: {} })}>{children}</a>
  ),
}));
jest.mock('../../config/GlobalConfigurations', () => ({ SideNavigationConfigurations: { header: {}, items: [] } }));

global.IS_REACT_ACT_ENVIRONMENT = true;

test('names the missing id, says nothing about requests, and leads back to the list', () => {
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  act(() => root.render(<AssessmentNotFound jobId="doesnotexist" />));
  expect(container.querySelector('h1').textContent).toBe('Assessment not found');
  expect(container.textContent).toContain('No assessment has the id doesnotexist.');
  expect(container.textContent).not.toMatch(/https?:|404|Failed to fetch/);
  // A link, so middle-click and "open in new tab" work and it announces as a link.
  const link = container.querySelector('a');
  expect(link.textContent).toBe('Go to assessments');
  expect(link.getAttribute('href')).toBe('/dashboard');
  act(() => link.click());
  expect(mockNavigate).toHaveBeenCalledWith('/dashboard');
  // The breadcrumb ends on the page the reader is on.
  const crumbs = [...container.querySelectorAll('li')].map((li) => li.textContent);
  expect(crumbs[crumbs.length - 1]).toBe('Assessment not found');
  act(() => root.unmount());
  container.remove();
});

test('the lookup shows a loading status while it is pending', () => {
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  act(() => root.render(<AssessmentLookupPending />));
  const status = container.querySelector('[role="status"]');
  expect(status).not.toBeNull();
  expect(status.textContent).toBe('Loading assessment…');
  act(() => root.unmount());
  container.remove();
});

test('the locale has the strings', () => {
  expect(en['assessment-not-found.title']).toBe('Assessment not found');
  expect(en['assessment-not-found.body']).toBe('No assessment has the id {{id}}.');
  expect(en['assessment-not-found.go-to-assessments']).toBe('Go to assessments');
  expect(en['assessment-not-found.breadcrumb']).toBe('Assessment not found');
  expect(en['assessment-lookup.loading']).toBe('Loading assessment…');
});

test('the three job routes sit behind the guard', () => {
  const routes = fs.readFileSync(path.join(__dirname, '..', '..', 'AppRoutes.js'), 'utf8');
  ['AnalysisResults', 'JobMonitoringSummary', 'AssignmentGate'].forEach((page) => {
    expect(routes.replace(/\s+/g, '')).toContain(`<JobRouteGuard><${page}/></JobRouteGuard>`);
  });
});
