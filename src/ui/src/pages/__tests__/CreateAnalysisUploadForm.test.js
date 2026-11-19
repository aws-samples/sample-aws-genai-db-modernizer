// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2327: the new-assessment page. With a local API it is one upload form with one
 * primary action, "Start assessment", that uploads the file and starts; the file stays
 * on this machine and no text says S3. A deployment that is not local opens on the
 * live-database Wizard. The name and the engine are filled in from the file, errors
 * are inline with focus on the first invalid field (no Flashbar), and a failed upload
 * says so in one plain sentence. The form, its fields and buttons are the real
 * Cloudscape components; only the page shell is a stand-in.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import createWrapper from '@cloudscape-design/components/test-utils/dom';
import en from '../../locales/en.json';
import CreateAnalysis from '../CreateAnalysis';
import { readApiLocal } from '../../utils/apiRuntime';
import { SELECTABLE_TARGET_ENGINES } from '../../utils/targetEngines';
import { ENGINE_LABELS } from '../../utils/engineCatalog.generated';
import { errorFromResponse } from '../../utils/apiErrors';

jest.mock('react-i18next', () => {
  const strings = jest.requireActual('../../locales/en.json');
  const t = (key, vars = {}) => (strings[key] ?? key).replace(/\{\{(\w+)\}\}/g, (_, name) => vars[name]);
  return { useTranslation: () => ({ t }) };
});
const mockNavigate = jest.fn();
jest.mock('react-router-dom', () => ({ useNavigate: () => mockNavigate }));
const mockExecute = jest.fn();
jest.mock('../../classes/ApiManager', () => ({
  __esModule: true,
  default: function ApiManager() {
    this.execute = mockExecute;
  },
}));
jest.mock('../../utils/apiRuntime', () => ({ readApiLocal: jest.fn() }));
jest.mock('../../components/AppHeader', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/app-layout', () => ({ __esModule: true, default: ({ content }) => <main>{content}</main> }));
jest.mock('@cloudscape-design/components/breadcrumb-group', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/side-navigation', () => ({ __esModule: true, default: () => null }));

global.IS_REACT_ACT_ENVIRONMENT = true;
global.ResizeObserver = global.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} };
global.matchMedia = global.matchMedia || (() => ({ matches: false, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }));

// The upload PUT: every instance answers with `putStatus` (0 is a network error).
let putStatus = 200;
let puts = [];
// When set, the PUT waits until the test calls releasePut().
let holdPut = false;
let releasePut = null;
class FakeXhr {
  constructor() { this.upload = {}; puts.push(this); }
  open(method, url) { this.method = method; this.url = url; }
  setRequestHeader() {}
  send(body) {
    this.body = body;
    const answer = () => (putStatus === 0 ? this.onerror() : (this.status = putStatus, this.onload()));
    if (holdPut) {
      releasePut = answer;
    } else {
      setTimeout(answer, 0);
    }
  }
}

const PREPARED = { success: true, job_id: 'job-9', upload_url: 'file:///tmp/a.json', upload_key: 'trailgear/job-9/uploads/collector-output.json' };
const apiAnswers = (overrides = {}) => async (calls) => {
  const { id } = calls[0];
  const answers = {
    prepare: PREPARED,
    confirm: { success: true, size_bytes: 10 },
    'create-assessment': { success: true, job_id: 'job-9' },
    ...overrides,
  };
  return { [id]: answers[id] };
};

let container;
let root;
let realXhr;

async function renderPage(local) {
  readApiLocal.mockResolvedValue(local);
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => { root.render(<CreateAnalysis />); });
}

beforeEach(() => {
  realXhr = global.XMLHttpRequest;
  global.XMLHttpRequest = FakeXhr;
  putStatus = 200;
  puts = [];
  holdPut = false;
  releasePut = null;
  mockExecute.mockImplementation(apiAnswers());
  jest.spyOn(console, 'error').mockImplementation(() => {});
  jest.spyOn(console, 'log').mockImplementation(() => {});
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  global.XMLHttpRequest = realXhr;
  mockExecute.mockReset();
  mockNavigate.mockReset();
  jest.restoreAllMocks();
});

const page = () => createWrapper(container);
const field = (label) => page().findAllFormFields().find((f) => f.findLabel()?.getElement().textContent.startsWith(label));
const button = (text) => page().findAllButtons().find((b) => b.getElement().textContent === text);
const checkbox = (label) => page().findAllCheckboxes().find((c) => c.findLabel().getElement().textContent.startsWith(label));
const calls = (id) => mockExecute.mock.calls.map((c) => c[0][0]).filter((c) => c.id === id);
const flush = () => act(async () => { await new Promise((r) => setTimeout(r, 0)); });

async function chooseFile(content, name = 'collection.json') {
  const file = new File([content], name, { type: 'application/json' });
  const input = page().findFileUpload().findNativeInput().getElement();
  Object.defineProperty(input, 'files', { value: [file], configurable: true });
  await act(async () => { input.dispatchEvent(new Event('change', { bubbles: true })); });
  await flush();
  await flush();
  return file;
}

function type(label, value) {
  const input = field(label).findControl().findInput().findNativeInput().getElement();
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
  act(() => {
    setter.call(input, value);
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

async function start() {
  await act(async () => { button(en['create-analysis.upload.start-button']).click(); });
  for (let i = 0; i < 4; i += 1) {
    await flush();
  }
}

const TRAILGEAR = 'collection_output\n{"tables": [], "queries": [], "metadata": {"database_name": "trailgear", "version": "PostgreSQL 16.4 on x86_64"}}';

describe('with a local API', () => {
  beforeEach(() => renderPage(true));

  test('the page is the upload form, with no mode choice and no Wizard', () => {
    expect(page().findTiles()).toBeNull();
    expect(page().findWizard()).toBeNull();
    expect(page().findForm()).not.toBeNull();
    expect(page().findFileUpload()).not.toBeNull();
    expect(container.textContent).toContain(en['create-analysis.header.title']);
  });

  test('the file is said to stay on this machine and nothing says S3', () => {
    expect(field(en['create-analysis.upload.file.label']).findDescription().getElement().textContent)
      .toBe('A .json file from the database collector. It stays on this machine, under the artifact folder.');
    expect(container.textContent).not.toMatch(/\bS3\b|presigned/i);
  });

  test('one primary action, Start assessment, at the right after Cancel', () => {
    const actions = page().findForm().findActions().findAllButtons();
    expect(actions.map((b) => b.getElement().textContent)).toEqual([
      en['common.actions.cancel'], en['create-analysis.upload.start-button'],
    ]);
    const primary = page().findAllButtons().filter((b) => b.getElement().className.includes('variant-primary'));
    expect(primary).toHaveLength(1);
    expect(primary[0].getElement().disabled).toBe(false);
    expect(button('Upload file')).toBeUndefined();
  });

  test('inputs start empty, with examples in the constraint text', () => {
    const name = field(en['create-analysis.source.database-name.label']);
    const input = name.findControl().findInput().findNativeInput().getElement();
    expect(input.value).toBe('');
    expect(input.getAttribute('placeholder')).toBeNull();
    expect(name.findConstraint().getElement().textContent).toMatch(/For example, wordpress/);
  });

  test('no Coming soon engine, and ElastiCache starts off with its reason', () => {
    expect(container.textContent).not.toMatch(/coming soon/i);
    SELECTABLE_TARGET_ENGINES.forEach((id) => {
      expect(checkbox(ENGINE_LABELS[id]).findNativeInput().getElement().checked).toBe(id !== 'elasticache');
    });
    expect(checkbox('ElastiCache').findDescription().getElement().textContent).toMatch(/off by default/);
  });

  test('a file that names its database and engine fills both in, editable', async () => {
    await chooseFile(TRAILGEAR);
    const input = field(en['create-analysis.source.database-name.label']).findControl().findInput().findNativeInput().getElement();
    expect(input.value).toBe('trailgear');
    expect(input.disabled).toBe(false);
    expect(field(en['create-analysis.upload.source-engine.label']).findControl().findSelect().findTrigger().getElement().textContent)
      .toBe('PostgreSQL (from file)');
    expect(container.textContent).toContain('Read from the file: PostgreSQL source, database trailgear');
    type(en['create-analysis.source.database-name.label'], 'trailgear_copy');
    expect(input.value).toBe('trailgear_copy');
  });

  test('a name the form would refuse is not filled in, and markup in it never renders', async () => {
    await chooseFile('{"metadata": {"database_name": "<img src=x onerror=alert(1)>", "version": "PostgreSQL 16"}}');
    expect(container.querySelector('img')).toBeNull();
    expect(field(en['create-analysis.source.database-name.label']).findControl().findInput().findNativeInput().getElement().value).toBe('');
    expect(container.textContent).not.toContain('<img');
    expect(container.textContent).toContain('Read from the file: PostgreSQL source');
  });

  test('a 5,000,000-character name is not filled in or shown', async () => {
    const long = 'a'.repeat(5000000);
    await chooseFile(JSON.stringify({ metadata: { database_name: long } }));
    expect(field(en['create-analysis.source.database-name.label']).findControl().findInput().findNativeInput().getElement().value).toBe('');
    expect(container.textContent.length).toBeLessThan(20000);
    expect(container.textContent).toContain(en['create-analysis.upload.file.read-none']);
  });

  test('a local run says starting from this page does not work yet, and where to go', () => {
    const notice = page().findAlert();
    expect(notice).not.toBeNull();
    expect(notice.getElement().textContent).toBe(en['create-analysis.upload.local-start-unavailable']);
    expect(notice.getElement().textContent).toMatch(/command line or with Claude Code/);
  });

  test('a second Start while one is in flight sends nothing more', async () => {
    holdPut = true;
    await chooseFile(TRAILGEAR);
    await start();
    expect(calls('prepare')).toHaveLength(1);
    // The loading button swallows clicks; submit the form itself, as Enter would.
    await act(async () => { container.querySelector('form').requestSubmit(); });
    await flush();
    expect(calls('prepare')).toHaveLength(1);
    expect(puts).toHaveLength(1);
    await act(async () => { releasePut(); });
    for (let i = 0; i < 4; i += 1) {
      await flush();
    }
    expect(calls('create-assessment')).toHaveLength(1);
  });

  test('during the upload the targets are locked and another file is ignored', async () => {
    holdPut = true;
    const first = await chooseFile(TRAILGEAR);
    await start();
    SELECTABLE_TARGET_ENGINES.forEach((id) => {
      expect(checkbox(ENGINE_LABELS[id]).findNativeInput().getElement().disabled).toBe(true);
    });
    await chooseFile('{"metadata": {"database_name": "other"}}', 'other.json');
    expect(field(en['create-analysis.source.database-name.label']).findControl().findInput().findNativeInput().getElement().value).toBe('trailgear');
    expect(container.textContent).not.toContain('other.json');
    await act(async () => { releasePut(); });
    for (let i = 0; i < 4; i += 1) {
      await flush();
    }
    expect(puts[0].body).toBe(first);
    expect(calls('create-assessment')[0].params.database_name).toBe('trailgear');
  });

  test('a file that names neither keeps the fields as they are', async () => {
    type(en['create-analysis.source.database-name.label'], 'shop');
    await chooseFile('{"tables": [], "queries": [], "metadata": {"version": "8.0.36"}}');
    expect(field(en['create-analysis.source.database-name.label']).findControl().findInput().findNativeInput().getElement().value).toBe('shop');
    expect(field(en['create-analysis.upload.source-engine.label']).findControl().findSelect().findTrigger().getElement().textContent)
      .toBe(en['create-analysis.offline.database-info.source-type-detect']);
    expect(container.textContent).toContain(en['create-analysis.upload.file.read-none']);
  });

  test('a name the user typed is not replaced by the file', async () => {
    type(en['create-analysis.source.database-name.label'], 'mine');
    await chooseFile(TRAILGEAR);
    expect(field(en['create-analysis.source.database-name.label']).findControl().findInput().findNativeInput().getElement().value).toBe('mine');
  });

  test('an empty submit shows inline errors, focuses the file and calls no API', async () => {
    await start();
    expect(field(en['create-analysis.upload.file.label']).findError().getElement().textContent).toBe(en['create-analysis.upload.file.error-required']);
    expect(field(en['create-analysis.source.database-name.label']).findError().getElement().textContent).toBe(en['create-analysis.source.database-name.error-required']);
    expect(document.activeElement).toBe(page().findFileUpload().findNativeInput().getElement());
    expect(page().findFlashbar()).toBeNull();
    expect(page().findForm().findError()).toBeNull();
    expect(mockExecute).not.toHaveBeenCalled();
  });

  test('with a file but no name, focus goes to the name', async () => {
    await chooseFile('{"metadata": {}}');
    await start();
    expect(field(en['create-analysis.upload.file.label']).findError()).toBeNull();
    expect(document.activeElement).toBe(field(en['create-analysis.source.database-name.label']).findControl().findInput().findNativeInput().getElement());
  });

  test('with no target engine ticked, focus goes to the first checkbox', async () => {
    await chooseFile(TRAILGEAR);
    SELECTABLE_TARGET_ENGINES.filter((id) => id !== 'elasticache').forEach((id) => act(() => checkbox(ENGINE_LABELS[id]).findNativeInput().getElement().click()));
    await start();
    expect(container.textContent).toContain(en['create-analysis.target-databases.error-required']);
    expect(document.activeElement).toBe(checkbox(ENGINE_LABELS[SELECTABLE_TARGET_ENGINES[0]]).findNativeInput().getElement());
    expect(mockExecute).not.toHaveBeenCalled();
  });

  test('Start assessment uploads the file, confirms it, starts and opens the job', async () => {
    const file = await chooseFile(TRAILGEAR);
    await start();
    expect(calls('prepare')[0].params).toEqual({ database_name: 'trailgear', source_database_type: 'postgresql' });
    expect(puts).toHaveLength(1);
    expect(puts[0].method).toBe('PUT');
    expect(puts[0].url).toBe(PREPARED.upload_url);
    expect(puts[0].body).toBe(file);
    expect(calls('confirm')[0].path).toBe('assessments/job-9/uploads/confirm?database_name=trailgear');
    expect(calls('create-assessment')[0].params).toEqual(expect.objectContaining({
      job_id: 'job-9',
      database_name: 'trailgear',
      source_database_type: 'postgresql',
      collection_mode: 'offline',
      offline_s3_key: PREPARED.upload_key,
      target_databases: SELECTABLE_TARGET_ENGINES.filter((id) => id !== 'elasticache'),
    }));
    expect(mockNavigate).toHaveBeenCalledWith('/analysis/monitor/summary/job-9');
  });

  test('a failed upload says so in one plain sentence and does not start', async () => {
    putStatus = 0;
    await chooseFile(TRAILGEAR);
    await start();
    const error = page().findForm().findError().getElement().textContent;
    // #2346: the shared helper's fixed message for the kind of failure.
    expect(error).toBe(`${en['create-analysis.error.upload-failed']}. ${en['errors.request.unreachable']}`);
    expect(error).not.toMatch(/https?:|file:|\b\d{3}\b|status/i);
    expect(calls('create-assessment')).toHaveLength(0);
    expect(mockNavigate).not.toHaveBeenCalled();
    expect(page().findFlashbar()).toBeNull();
  });

  test('a confirm the API refuses is an upload failure, without its detail', async () => {
    mockExecute.mockImplementation(apiAnswers({
      confirm: { success: false, status: 404, error: errorFromResponse(404, 'Not Found', JSON.stringify({ detail: 'Upload not found at trailgear/job-9/uploads/collector-output.json' })) },
    }));
    await chooseFile(TRAILGEAR);
    await start();
    const error = page().findForm().findError().getElement().textContent;
    expect(error).toBe(`${en['create-analysis.error.upload-failed']}. ${en['errors.request.not-found']}`);
    expect(error).not.toContain('collector-output.json');
  });

  test('a start the API refuses says the assessment could not start', async () => {
    mockExecute.mockImplementation(apiAnswers({
      'create-assessment': { success: false, status: 500, error: errorFromResponse(500, 'Internal Server Error', '') },
    }));
    await chooseFile(TRAILGEAR);
    await start();
    expect(page().findForm().findError().getElement().textContent).toBe(`${en['create-analysis.error.start-failed']}. ${en['errors.request.server']}`);
    expect(mockNavigate).not.toHaveBeenCalled();
  });

  test("a start the API refuses with a reason shows that reason", async () => {
    mockExecute.mockImplementation(apiAnswers({
      'create-assessment': { success: false, status: 409, error: errorFromResponse(409, 'Conflict', JSON.stringify({ detail: 'Job job-9 was already started.' })) },
    }));
    await chooseFile(TRAILGEAR);
    await start();
    const error = page().findForm().findError().getElement().textContent;
    expect(error).toBe(`${en['create-analysis.error.start-failed']}. Job job-9 was already started.`);
    expect(error).not.toMatch(/409/);
  });

  test('a refusal whose text is not a safe sentence shows the fixed message', async () => {
    mockExecute.mockImplementation(apiAnswers({
      'create-assessment': { success: false, status: 409, error: errorFromResponse(409, 'Conflict', JSON.stringify({ detail: 'see http://10.0.0.5:8000/x <b>' })) },
    }));
    await chooseFile(TRAILGEAR);
    await start();
    const error = page().findForm().findError().getElement().textContent;
    expect(error).toBe(`${en['create-analysis.error.start-failed']}. ${en['errors.request.rejected-form']}`);
    expect(error).not.toMatch(/10\.0\.0\.5|\b8000\b|http/);
  });

  test('Cancel goes back to the dashboard', () => {
    act(() => button(en['common.actions.cancel']).click());
    expect(mockNavigate).toHaveBeenCalledWith('/dashboard');
    expect(mockExecute).not.toHaveBeenCalled();
  });
});

describe('with an API that is not local', () => {
  beforeEach(() => renderPage(false));

  test('the page opens on the live-database Wizard, with the upload one tile away', async () => {
    const tiles = page().findTiles();
    expect(tiles).not.toBeNull();
    expect(page().findWizard()).not.toBeNull();
    expect(tiles.findInputByValue('online').getElement().checked).toBe(true);
    act(() => tiles.findInputByValue('upload').getElement().click());
    expect(page().findWizard()).toBeNull();
    expect(field(en['create-analysis.upload.file.label']).findDescription().getElement().textContent)
      .toBe(en['create-analysis.upload.file.description-deployed']);
    expect(page().findAlert()).toBeNull();
  });

  test('Next on an empty first step shows inline errors and no Flashbar', async () => {
    await act(async () => { page().findWizard().findPrimaryButton().click(); });
    await flush();
    expect(field(en['create-analysis.source.host.label']).findError().getElement().textContent).toBe(en['create-analysis.source.host.error-required']);
    expect(page().findFlashbar()).toBeNull();
    expect(document.activeElement).toBe(field(en['create-analysis.source.host.label']).findControl().findInput().findNativeInput().getElement());
  });

  test('the live inputs start empty, with examples in the constraint text', () => {
    [en['create-analysis.source.host.label'], en['create-analysis.source.port.label']].forEach((label) => {
      const input = field(label).findControl().findInput().findNativeInput().getElement();
      expect(input.value).toBe('');
      expect(input.getAttribute('placeholder')).toBeNull();
      expect(field(label).findConstraint().getElement().textContent).toMatch(/For example/);
    });
  });
});

test('an API that does not answer gets the upload form, with no claim where the file goes', async () => {
  await renderPage(null);
  expect(page().findTiles()).toBeNull();
  expect(page().findForm()).not.toBeNull();
  expect(field(en['create-analysis.upload.file.label']).findDescription().getElement().textContent)
    .toBe(en['create-analysis.upload.file.description-unknown']);
  expect(container.textContent).not.toMatch(/stays on this machine/);
  expect(page().findAlert()).toBeNull();
});
