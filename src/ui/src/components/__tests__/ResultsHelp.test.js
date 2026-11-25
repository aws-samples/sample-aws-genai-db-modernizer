// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2512: the results page's Info links and help panels, on the real Cloudscape
 * components and the real English strings. A section header's Info link opens its
 * topic in the AppLayout's help drawer, which shares the tools area with "On this
 * page" (#2313). Each panel holds the explanation its section used to state inline and
 * ends with a link to the matching help site section. The page itself cannot render
 * under this repo's Jest config (AnalysisResultsTabs.test.js reads its wiring), so a
 * small AppLayout here carries the same drawers the page builds.
 */
import fs from 'fs';
import path from 'path';
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import AppLayout from '@cloudscape-design/components/app-layout';
import i18n from '../../i18n';
import en from '../../locales/en.json';
import HelpInfoLink from '../HelpInfoLink';
import ResultsHelpPanel, { RESULTS_HELP_PANEL_ID } from '../ResultsHelpPanel';
import useResultsDrawers, { ResultsHelp, resultsDrawers, shownDrawerId } from '../../hooks/useResultsDrawers';
import {
  DEFAULT_HELP_TOPIC, HELP_DRAWER_ID, HELP_TOPIC_IDS, ON_THIS_PAGE_DRAWER_ID,
  helpPanelContent, helpTopicUrl, resultsHelpContext,
} from '../../utils/resultsHelp';
import { HELP_SITE_URL, RESULTS_HELP_URL } from '../../utils/helpSite';
import { WORKLOAD_FIT_DEFINITION } from '../../utils/workloadFitLabels';

global.IS_REACT_ACT_ENVIRONMENT = true;
global.ResizeObserver = global.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} };

const REPO = path.resolve(__dirname, '../../../../..');
const DOC = fs.readFileSync(path.join(REPO, 'docs', 'understand-the-results.md'), 'utf8');
const t = i18n.t.bind(i18n);

let container;
let root;
let wide = false;

beforeEach(() => {
  wide = false;
  global.matchMedia = () => ({ matches: wide, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} });
  window.matchMedia = global.matchMedia;
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

// mkdocs' toc slug for a "## Heading" line.
const slug = heading => heading.toLowerCase().replace(/[^\w\s-]/g, '').trim().replace(/\s+/g, '-');
const DOC_ANCHORS = DOC.split('\n').filter(l => l.startsWith('## ')).map(l => slug(l.slice(3)));

describe('the help topics', () => {
  test('each topic has a title and text in en.json and links to a heading of the results help page', () => {
    expect(HELP_TOPIC_IDS).toEqual(expect.arrayContaining([
      'results-page', 'migration-roadmap', 'close-calls', 'query-flow', 'query-routing', 'table-owners', 'unservable',
    ]));
    HELP_TOPIC_IDS.forEach((id) => {
      expect(en[`analysis-results.help.${id}.title`]).toBeTruthy();
      const content = helpPanelContent(id, t, resultsHelpContext(null));
      expect(content.paragraphs.length).toBeGreaterThan(0);
      content.paragraphs.forEach(p => expect(p).not.toMatch(/\{\{|—/));
      const url = helpTopicUrl(id);
      expect(url.startsWith(`${HELP_SITE_URL}understand-the-results/#`)).toBe(true);
      expect(DOC_ANCHORS).toContain(url.split('#')[1]);
    });
    expect(helpTopicUrl(DEFAULT_HELP_TOPIC)).toBe(RESULTS_HELP_URL);
  });

  test('the roadmap panel holds the step, stage and unit definitions and the readiness rule the section used to state', () => {
    const { paragraphs } = helpPanelContent('migration-roadmap', t, { adoptionShown: true, readinessShown: true });
    expect(paragraphs).toContain(en['analysis-results.adoption.definitions']);
    expect(paragraphs).toContain(en['analysis-results.waves.readiness-rule']);
    expect(paragraphs.join(' ')).toMatch(/co-dependent/);
    // As in the section: the definitions with adoption paths, the rule with readiness lines.
    const older = helpPanelContent('migration-roadmap', t, resultsHelpContext({ facts: {} })).paragraphs;
    expect(older).not.toContain(en['analysis-results.adoption.definitions']);
    expect(older).not.toContain(en['analysis-results.waves.readiness-rule']);
  });

  test('the step scores panel states how each score reads, in the facts words (#2288)', () => {
    const body = en['analysis-results.help.step-scores.body'];
    const plan = { score_scales: {
      gravity: { high_from: 0.5, band_labels: { low: 'low', high: 'high' }, note: 'Sentinel gravity note.' },
      hub_score: { high_from: 0.5, band_labels: { low: 'low', high: 'high' }, note: 'Sentinel hub note.' },
    } };
    const { paragraphs, url } = helpPanelContent('step-scores', t, resultsHelpContext({ facts: { slice_plan: plan } }));
    expect(paragraphs).toEqual([
      body,
      { text: 'Sentinel gravity note.', figure: 'step-score-note' },
      { text: 'Sentinel hub note.', figure: 'step-score-note' },
    ]);
    expect(url.endsWith('#adoption-paths')).toBe(true);
    // A report with no scales states the page's line alone.
    expect(helpPanelContent('step-scores', t, resultsHelpContext({ facts: {} })).paragraphs).toEqual([body]);
  });

  test('the table owners panel holds the owner rule the report follows and, with a CDC copy, the CDC sentence', () => {
    const body = en['analysis-results.help.table-owners.body'];
    const intro = en['analysis-results.sources-of-truth.intro'];
    const replicated = { written_table_count: 1, replicated_tables: [{ table: 'app.t' }] };
    const current = { facts: { table_owner_lines: { table_count: 2, unwritten_owner_rule: 'relational_step' }, table_sources: replicated } };
    const old = { facts: { table_owner_lines: { table_count: 2, unwritten_owner_rule: 'most_queries' }, table_sources: replicated } };
    expect(helpPanelContent('table-owners', t, resultsHelpContext(current)).paragraphs).toEqual([
      body, en['analysis-results.table-owners.rule'], intro,
    ]);
    expect(helpPanelContent('table-owners', t, resultsHelpContext(old)).paragraphs).toEqual([
      body, en['analysis-results.table-owners.rule-most-queries'], intro,
    ]);
    // Only a shared risk sentence (a CDC copy of a table nothing writes, #2459) still has the note.
    const sharedOnly = { facts: { table_sources: { written_table_count: 1, replicated_tables: [], shared_risk_sentence: 'Every table another engine reads through CDC has this risk.' } } };
    expect(helpPanelContent('table-owners', t, resultsHelpContext(sharedOnly)).paragraphs).toEqual([
      body, intro, { text: 'Every table another engine reads through CDC has this risk.', figure: 'shared-risk' },
    ]);
    // A report with no owner lines states no owner rule on the page, so neither does the panel.
    expect(helpPanelContent('table-owners', t, resultsHelpContext({ facts: {} })).paragraphs).toEqual([body]);
  });

  test('a report with no replicated table and no shared risk sentence gets no CDC sentence, as its section had no note', () => {
    const none = {
      facts: {
        table_owner_lines: { table_count: 1, unwritten_owner_rule: 'relational_step' },
        table_sources: { written_table_count: 1, replicated_tables: [], shared_risk_sentence: null },
      },
    };
    const { paragraphs } = helpPanelContent('table-owners', t, resultsHelpContext(none));
    expect(paragraphs).not.toContain(en['analysis-results.sources-of-truth.intro']);
    expect(paragraphs).toContain(en['analysis-results.table-owners.rule']);
  });

  test('the table owners panel states the report\'s risk sentence as written, and an older report\'s own risks', () => {
    const shared = 'Every replicated table above has this risk. Stored sentence, as the report writes it.';
    const synthesis = { facts: { table_sources: {
      written_table_count: 2,
      replicated_tables: [
        { table: 'app.a', risk: 'Eventually consistent: a.', shares_replica_risk: true },
        { table: 'app.b', risk: 'Eventually consistent: b.', shares_replica_risk: false },
      ],
      shared_risk_sentence: shared,
    } } };
    const { paragraphs } = helpPanelContent('table-owners', t, resultsHelpContext(synthesis));
    expect(paragraphs).toContainEqual({ text: shared, figure: 'shared-risk' });
    expect(paragraphs).toContainEqual({ text: 'app.b Eventually consistent: b.', own: 'app.b' });
    expect(JSON.stringify(paragraphs)).not.toContain('app.a Eventually');
    // Paragraphs are keyed by position and table, never by their text.
    act(() => root.render(<ResultsHelpPanel topic="table-owners" context={resultsHelpContext(synthesis)} t={t} />));
    expect(container.querySelector('[data-own-risk="app.b"]').textContent).toBe('app.b Eventually consistent: b.');
  });

  test('the unservable panel holds the re-run steps for the kinds the alert lists', () => {
    const base = { unservable_query_count: 1, unservable_queries: [{ query_id: 'q' }] };
    const onEngine = { facts: { unservable: { ...base, no_fit_query_count: 1 } } };
    const scoped = { facts: { unservable: { ...base, no_engine_in_scope_query_count: 1, no_engine_in_scope_closest_engines: ['dynamodb'] } } };
    const body = en['analysis-results.help.unservable.body'];
    expect(helpPanelContent('unservable', t, resultsHelpContext(onEngine)).paragraphs)
      .toEqual([body, en['analysis-results.unservable.rerun']]);
    expect(helpPanelContent('unservable', t, resultsHelpContext(scoped)).paragraphs)
      .toEqual([body, 'To serve them, start a new assessment whose scope adds DynamoDB, or keep them on the source database.']);
    expect(helpPanelContent('unservable', t, resultsHelpContext({ facts: {} })).paragraphs).toEqual([body]);
    // True for a query on a fallback engine and for one on no engine alike.
    expect(body).not.toMatch(/stays where it is/);
    expect(body).toMatch(/no engine can serve/);
  });

  test('the query routing panel states the shared workload fit definition', () => {
    expect(helpPanelContent('query-routing', t).paragraphs).toContain(WORKLOAD_FIT_DEFINITION);
  });

  test('the query flow panel explains the cache layer', () => {
    expect(helpPanelContent('query-flow', t).paragraphs.join(' ')).toMatch(/cache layer/i);
  });

  test('an unknown topic falls back to the results page topic', () => {
    expect(helpPanelContent('nope', t).id).toBe(DEFAULT_HELP_TOPIC);
  });
});

describe('the help panel', () => {
  test('it has the topic title as its heading, the text and a help site link that opens in a new tab', () => {
    act(() => root.render(<ResultsHelpPanel topic="close-calls" context={{}} t={t} />));
    expect(container.querySelector('h2').textContent).toBe('Close calls');
    const body = container.querySelector(`#${RESULTS_HELP_PANEL_ID}`);
    expect(body.getAttribute('data-help-topic')).toBe('close-calls');
    expect(body.textContent).toContain(en['analysis-results.help.close-calls.body']);
    const link = [...container.querySelectorAll('a')].find(a => a.getAttribute('href') === helpTopicUrl('close-calls'));
    expect(link).toBeTruthy();
    expect(link.textContent).toContain('Close calls on the help site');
    expect(link.getAttribute('target')).toBe('_blank');
    expect(container.querySelector('h3').textContent).toBe('Learn more');
  });
});

describe('the Info link', () => {
  test('a link can carry its own label and still opens its topic', () => {
    act(() => root.render(
      <ResultsHelp.Provider value={() => {}}>
        <HelpInfoLink topic="table-owners" t={t} ariaLabel="Info about the consistency of replica reads" />
      </ResultsHelp.Provider>,
    ));
    expect(container.querySelector('[data-help-topic="table-owners"]').getAttribute('aria-label'))
      .toBe('Info about the consistency of replica reads');
  });

  test('outside the results page it renders nothing', () => {
    act(() => root.render(<HelpInfoLink topic="close-calls" t={t} />));
    expect(container.innerHTML).toBe('');
  });

  test('it is a labelled button that opens its topic from a click and from the keyboard', () => {
    const opened = [];
    act(() => root.render(
      <ResultsHelp.Provider value={topic => opened.push(topic)}>
        <HelpInfoLink topic="table-owners" t={t} />
      </ResultsHelp.Provider>,
    ));
    const link = container.querySelector('[data-help-topic="table-owners"]');
    // Cloudscape adds a colon for screen readers after the visible "Info".
    expect(link.textContent).toMatch(/^Info/);
    expect(link.getAttribute('aria-label')).toBe('Info about the Table owners section');
    expect(link.getAttribute('role')).toBe('button');
    expect(link.getAttribute('tabindex')).toBe('0');
    act(() => link.click());
    act(() => link.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', keyCode: 13, bubbles: true })));
    expect(opened).toEqual(['table-owners', 'table-owners']);
  });
});

describe('the drawers', () => {
  test('On this page is listed only when the tab has it, and the help panel always, each with its labels', () => {
    const both = resultsDrawers({ t, onThisPage: <nav />, help: <div /> });
    expect(both.map(d => d.id)).toEqual([ON_THIS_PAGE_DRAWER_ID, HELP_DRAWER_ID]);
    expect(both[0].ariaLabels).toEqual({ drawerName: 'On this page', triggerButton: 'Open On this page', closeButton: 'Close On this page' });
    expect(both[1].ariaLabels).toEqual({ drawerName: 'Help panel', triggerButton: 'Open the help panel', closeButton: 'Close the help panel' });
    const helpOnly = resultsDrawers({ t, onThisPage: null, help: <div /> });
    expect(helpOnly.map(d => d.id)).toEqual([HELP_DRAWER_ID]);
    expect(shownDrawerId(ON_THIS_PAGE_DRAWER_ID, helpOnly)).toBeNull();
    expect(shownDrawerId(ON_THIS_PAGE_DRAWER_ID, both)).toBe(ON_THIS_PAGE_DRAWER_ID);
    expect(shownDrawerId(HELP_DRAWER_ID, helpOnly)).toBe(HELP_DRAWER_ID);
  });

  // The page's wiring, in a small AppLayout: an Info link in the content opens its topic
  // in the help drawer, in place of On this page, and focuses the drawer.
  function Page({ focused }) {
    const { activeDrawerId, helpTopic, openHelp, onDrawerChange, layoutRef } = useResultsDrawers();
    const ref = (value) => {
      layoutRef.current = value && { ...value, focusActiveDrawer: () => { focused.push(helpTopic); value.focusActiveDrawer(); } };
    };
    const drawers = resultsDrawers({
      t,
      onThisPage: <nav aria-label="On this page">sections</nav>,
      help: <ResultsHelpPanel topic={helpTopic} context={{}} t={t} />,
    });
    return (
      <ResultsHelp.Provider value={openHelp}>
        <AppLayout
          ref={ref}
          navigationHide
          content={<h1>Results <HelpInfoLink topic="close-calls" t={t} /></h1>}
          drawers={drawers}
          activeDrawerId={shownDrawerId(activeDrawerId, drawers)}
          onDrawerChange={onDrawerChange}
        />
      </ResultsHelp.Provider>
    );
  }

  test('On this page opens with the page on a wide screen and stays closed on a narrow one', () => {
    wide = true;
    act(() => root.render(<Page focused={[]} />));
    expect(container.querySelector('nav[aria-label="On this page"]')).not.toBeNull();
    act(() => root.unmount());
    root = createRoot(container);
    wide = false;
    act(() => root.render(<Page focused={[]} />));
    expect(container.querySelector('nav[aria-label="On this page"]')).toBeNull();
    expect(container.querySelector(`#${RESULTS_HELP_PANEL_ID}`)).toBeNull();
  });

  test('an Info link opens its topic in the help drawer, in place of On this page, and focuses it', async () => {
    wide = true;
    const focused = [];
    act(() => root.render(<Page focused={focused} />));
    await act(async () => {
      container.querySelector('[data-help-topic="close-calls"]').click();
    });
    await act(async () => {
      await new Promise(resolve => requestAnimationFrame(resolve));
    });
    const panel = container.querySelector(`#${RESULTS_HELP_PANEL_ID}`);
    expect(panel.getAttribute('data-help-topic')).toBe('close-calls');
    expect(container.querySelector('nav[aria-label="On this page"]')).toBeNull();
    expect(focused).toEqual(['close-calls']);
  });
});

describe('closing the help drawer', () => {
  function Page() {
    const { activeDrawerId, helpTopic, openHelp, onDrawerChange, layoutRef } = useResultsDrawers();
    const drawers = resultsDrawers({ t, onThisPage: null, help: <ResultsHelpPanel topic={helpTopic} context={{}} t={t} /> });
    return (
      <ResultsHelp.Provider value={openHelp}>
        <AppLayout
          ref={layoutRef}
          navigationHide
          content={<h1>Results <HelpInfoLink topic="query-flow" t={t} /></h1>}
          drawers={drawers}
          activeDrawerId={shownDrawerId(activeDrawerId, drawers)}
          onDrawerChange={onDrawerChange}
        />
      </ResultsHelp.Provider>
    );
  }
  const frame = () => new Promise(resolve => requestAnimationFrame(resolve));

  test('focus returns to the Info link that opened it', async () => {
    act(() => root.render(<Page />));
    const link = () => container.querySelector('[data-help-topic="query-flow"]');
    await act(async () => {
      link().focus();
      link().click();
    });
    await act(async () => { await frame(); await frame(); });
    expect(container.querySelector(`#${RESULTS_HELP_PANEL_ID}`).getAttribute('data-help-topic')).toBe('query-flow');
    expect(document.activeElement).not.toBe(link());
    const close = container.querySelector('button[aria-label="Close the help panel"]');
    expect(close).not.toBeNull();
    await act(async () => { close.click(); });
    await act(async () => { await frame(); await frame(); });
    expect(container.querySelector(`#${RESULTS_HELP_PANEL_ID}`)).toBeNull();
    expect(document.activeElement).toBe(link());
  });
});
