// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2313: the results page's tabs follow the URL and write it. The hook runs here in a
 * real router (MemoryRouter) with a stand-in page that, like Cloudscape's Tabs, renders
 * only the active tab's sections, so a link to a section on another tab only works when
 * the hook opens that tab first.
 */
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { MemoryRouter, useLocation, useNavigate } from 'react-router-dom';
import useResultsTabs, { useSectionsOnPage } from '../useResultsTabs';
import { RESULTS_TABS } from '../../utils/resultsTabs';

global.IS_REACT_ACT_ENVIRONMENT = true;

let container;
let root;
let scrolled;
let rafSpy;

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  scrolled = [];
  Element.prototype.scrollIntoView = function scrollIntoView() { scrolled.push(this.id); };
  rafSpy = jest.spyOn(window, 'requestAnimationFrame').mockImplementation((cb) => { cb(); return 1; });
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  rafSpy.mockRestore();
  delete Element.prototype.scrollIntoView;
});

function Page({ ready, sections }) {
  const { activeTab, selectTab, goToSection } = useResultsTabs({ ready });
  const location = useLocation();
  const navigate = useNavigate();
  const tab = RESULTS_TABS.find(x => x.id === activeTab);
  const shown = tab.sections.map(s => s.id).filter(id => !sections || sections.includes(id));
  return (
    <div>
      <output data-testid="tab">{activeTab}</output>
      <output data-testid="url">{`${location.search}${location.hash}`}</output>
      {RESULTS_TABS.map(x => (
        <button key={x.id} type="button" data-tab={x.id} onClick={() => selectTab(x.id)} />
      ))}
      <button type="button" data-go="table-design-section" onClick={() => goToSection('table-design-section')} />
      <button type="button" data-go="target-engines" onClick={() => goToSection('target-engines')} />
      <button type="button" data-back onClick={() => navigate(-1)} />
      <a href="#close-calls-section" data-link onClick={(e) => { e.preventDefault(); navigate({ search: location.search, hash: '#close-calls-section' }); }}>x</a>
      <div data-panel={activeTab}>
        {shown.map(id => <div key={id} id={id} />)}
        {activeTab === 'summary' && <div id="target-engines" />}
      </div>
    </div>
  );
}

function mount(path, { ready = true, sections } = {}) {
  act(() => {
    root.render(
      <MemoryRouter initialEntries={[path]}>
        <Page ready={ready} sections={sections} />
      </MemoryRouter>,
    );
  });
}

const tab = () => container.querySelector('[data-testid=tab]').textContent;
const url = () => container.querySelector('[data-testid=url]').textContent;
const click = (selector) => act(() => { container.querySelector(selector).click(); });

describe('the tab follows the URL', () => {
  test('no tab in the URL opens the summary', () => {
    mount('/analysis/results/j1');
    expect(tab()).toBe('summary');
    expect(container.querySelector('#executive-summary-section')).not.toBeNull();
    expect(scrolled).toEqual([]);
  });

  test('?tab= opens that tab', () => {
    mount('/analysis/results/j1?tab=plan');
    expect(tab()).toBe('plan');
    expect(container.querySelector('#migration-roadmap-section')).not.toBeNull();
    expect(container.querySelector('#executive-summary-section')).toBeNull();
  });

  test('an unknown tab opens the summary', () => {
    mount('/analysis/results/j1?tab=nope');
    expect(tab()).toBe('summary');
  });
});

describe('changing tab writes the URL', () => {
  test('the URL names the tab chosen, and back returns to the one before', () => {
    mount('/analysis/results/j1');
    click('[data-tab=risks]');
    expect(tab()).toBe('risks');
    expect(url()).toBe('?tab=risks');
    click('[data-tab=data-model]');
    expect(url()).toBe('?tab=data-model');
    click('[data-back]');
    expect(tab()).toBe('risks');
  });

  test('other parameters stay and the section hash goes: it named a section of the old tab', () => {
    mount('/analysis/results/j1?engine=dynamodb#access-patterns-section');
    click('[data-tab=plan]');
    expect(url()).toBe('?engine=dynamodb&tab=plan');
  });

  test('changing tab scrolls nowhere', () => {
    mount('/analysis/results/j1');
    click('[data-tab=queries]');
    expect(scrolled).toEqual([]);
  });
});

describe('a link to a section lands on its tab and at the section', () => {
  test('an old anchor redirects to its tab, then scrolls to the section and focuses it', () => {
    mount('/analysis/results/j1#table-owners-section');
    expect(tab()).toBe('data-model');
    expect(url()).toBe('?tab=data-model#table-owners-section');
    expect(scrolled).toEqual(['table-owners-section']);
    const el = container.querySelector('#table-owners-section');
    expect(document.activeElement).toBe(el);
    expect(el.getAttribute('tabindex')).toBe('-1');
  });

  test('the ?engine= link (#2300) opens the Queries tab at the query table (#2446)', () => {
    mount('/analysis/results/j1?engine=dynamodb#query-routing-section');
    expect(tab()).toBe('queries');
    expect(url()).toBe('?engine=dynamodb&tab=queries#query-routing-section');
    expect(scrolled).toEqual(['query-routing-section']);
    expect(document.activeElement.id).toBe('query-routing-section');
  });

  test('an old link to the access pattern explorer lands on the query table that holds its rows (#2446)', () => {
    mount('/analysis/results/j1?engine=dynamodb#access-patterns-section');
    expect(tab()).toBe('queries');
    expect(url()).toBe('?engine=dynamodb&tab=queries#access-patterns-section');
    expect(scrolled).toEqual(['query-routing-section']);
    expect(document.activeElement.id).toBe('query-routing-section');
  });

  test('the section a hash names wins over a stale tab in the query', () => {
    mount('/analysis/results/j1?tab=plan#table-design-section');
    expect(tab()).toBe('data-model');
    expect(url()).toBe('?tab=data-model#table-design-section');
    expect(scrolled).toEqual(['table-design-section']);
  });

  test('an anchor inside a section lands on its tab', () => {
    mount('/analysis/results/j1?tab=plan#target-engines');
    expect(tab()).toBe('summary');
    expect(scrolled).toEqual(['target-engines']);
  });

  test('nothing scrolls until the results are on the page', () => {
    mount('/analysis/results/j1#close-calls-section', { ready: false });
    expect(tab()).toBe('plan');
    expect(scrolled).toEqual([]);
    mount('/analysis/results/j1#close-calls-section', { ready: true });
    expect(scrolled).toEqual(['close-calls-section']);
  });

  test('a hash link followed on the page opens its tab and scrolls there', () => {
    mount('/analysis/results/j1?tab=queries');
    act(() => { container.querySelector('[data-link]').dispatchEvent(new MouseEvent('click', { bubbles: true })); });
    expect(tab()).toBe('plan');
    expect(url()).toBe('?tab=plan#close-calls-section');
    expect(scrolled).toEqual(['close-calls-section']);
  });

  test('a section the page does not show leaves the tab open and scrolls nowhere', () => {
    mount('/analysis/results/j1#table-design-section', { sections: [] });
    expect(tab()).toBe('data-model');
    expect(scrolled).toEqual([]);
  });
});

describe('going to a section from the page', () => {
  test('opens the section\'s tab, names it in the URL and scrolls to it', () => {
    mount('/analysis/results/j1?engine=aurora_mysql');
    goTo('table-design-section');
    expect(tab()).toBe('data-model');
    expect(url()).toBe('?engine=aurora_mysql&tab=data-model#table-design-section');
    expect(scrolled).toEqual(['table-design-section']);
    expect(document.activeElement.id).toBe('table-design-section');
  });

  test('going to the same section again scrolls again (the reader may have scrolled away)', () => {
    mount('/analysis/results/j1');
    goTo('table-design-section');
    goTo('table-design-section');
    expect(scrolled).toEqual(['table-design-section', 'table-design-section']);
  });

  test('to another tab adds a history entry, so back returns to the tab the reader came from', () => {
    mount('/analysis/results/j1');
    click('[data-tab=plan]');
    goTo('target-engines');
    expect(tab()).toBe('summary');
    expect(url()).toBe('?tab=summary#target-engines');
    click('[data-back]');
    expect(tab()).toBe('plan');
    expect(url()).toBe('?tab=plan');
    click('[data-back]');
    expect(url()).toBe('');
  });

  test('within the open tab (On this page) replaces the entry: back leaves the tab', () => {
    mount('/analysis/results/j1');
    click('[data-tab=summary]');
    goTo('target-engines');
    expect(url()).toBe('?tab=summary#target-engines');
    goTo('target-engines');
    click('[data-back]');
    expect(url()).toBe('');
  });
});

function goTo(id) {
  click(`[data-go="${id}"]`);
}

describe('the sections on the page', () => {
  function Probe({ tabId, present }) {
    const [root, setRoot] = React.useState(null);
    const ids = useSectionsOnPage(tabId, root);
    return (
      <div>
        <output data-testid="ids">{ids.join(',')}</output>
        <div ref={setRoot}>{present.map(id => <div key={id} id={id} />)}</div>
      </div>
    );
  }

  const ids = () => container.querySelector('[data-testid=ids]').textContent;

  test('are the tab\'s sections that rendered, in the tab\'s order', async () => {
    await act(async () => { root.render(<Probe tabId="plan" present={['close-calls-section', 'migration-roadmap-section', 'table-design-section']} />); });
    expect(ids()).toBe('migration-roadmap-section,close-calls-section');
  });

  test('follow a section that renders later (its data arrives after the page)', async () => {
    await act(async () => { root.render(<Probe tabId="data-model" present={['table-owners-section']} />); });
    expect(ids()).toBe('table-owners-section');
    await act(async () => { root.render(<Probe tabId="data-model" present={['table-owners-section', 'source-tables-section']} />); });
    expect(ids()).toBe('table-owners-section,source-tables-section');
  });

  test('start watching once the content is rendered (it is not while the page loads)', async () => {
    function Late({ shown }) {
      const [root, setRoot] = React.useState(null);
      const found = useSectionsOnPage('queries', root);
      return (
        <div>
          <output data-testid="ids">{found.join(',')}</output>
          {shown && <div ref={setRoot}><div id="query-routing-section" /></div>}
        </div>
      );
    }
    await act(async () => { root.render(<Late shown={false} />); });
    expect(ids()).toBe('');
    await act(async () => { root.render(<Late shown />); });
    expect(ids()).toBe('query-routing-section');
  });

  test('change with the tab', async () => {
    await act(async () => { root.render(<Probe tabId="plan" present={['migration-roadmap-section', 'table-design-section']} />); });
    expect(ids()).toBe('migration-roadmap-section');
    await act(async () => { root.render(<Probe tabId="data-model" present={['migration-roadmap-section', 'table-design-section']} />); });
    expect(ids()).toBe('table-design-section');
  });
});
