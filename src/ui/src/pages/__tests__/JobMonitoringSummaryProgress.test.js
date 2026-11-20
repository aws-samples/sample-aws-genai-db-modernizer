// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2317: the progress page. A finished assessment shows what it found (database, source,
 * target engines, monthly cost, overall risk, read from the report's facts) and "View
 * results" as the primary button in the header; the phase tracker is for a running job,
 * as Cloudscape Steps. The query flow's root says "queries". Renders the real Cloudscape
 * components and the real English strings; only the page shell is stubbed.
 */
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import '../../i18n';
import JobMonitoringSummary from '../JobMonitoringSummary';
import ApiManager from '../../classes/ApiManager';

jest.mock('../../classes/ApiManager');
jest.mock('react-router-dom', () => ({
  useParams: () => ({ jobId: 'job-1' }),
  useNavigate: () => jest.fn(),
}));
jest.mock('../../components/AppHeader', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/app-layout', () => ({
  __esModule: true,
  default: ({ content }) => <main>{content}</main>,
}));
jest.mock('@cloudscape-design/components/breadcrumb-group', () => ({ __esModule: true, default: () => null }));
jest.mock('@cloudscape-design/components/side-navigation', () => ({ __esModule: true, default: () => null }));

global.IS_REACT_ACT_ENVIRONMENT = true;
global.ResizeObserver = global.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} };

const stage = (name, status = 'completed') => ({ name, status });
const COMPLETE_STAGES = [
  stage('RunCollector'),
  stage('RunRefereeTriage'),
  stage('WaitForAssignmentApproval'),
  stage('RunSchemaDesign'),
  stage('RunRefereeSynthesis'),
];
const RUNNING_STAGES = [stage('RunCollector'), stage('RunRefereeTriage', 'in-progress')];

const SYNTHESIS = {
  cache_overlay: { engine: 'elasticache' },
  risk_assessment: { overall_risk_level: 'HIGH' },
  facts: {
    projected_monthly_cost_usd: 1234.5,
    target_engines: ['aurora_mysql', 'dynamodb', 'elasticache'],
    engine_query_counts: { aurora_mysql: 1040, dynamodb: 60 },
    in_scope_query_count: 1100,
    // The brief's level can sit below the assessment's; the page prints the assessment's.
    executive_brief: { overall_risk_level: 'LOW' },
  },
};

function respond({ stages, synthesis, currentStage }) {
  return async (calls) => {
    const out = {};
    calls.forEach(({ id }) => {
      if (id === 'get-job-details') {
        out[id] = {
          success: true,
          database_name: 'wordpress',
          source_database_type: 'mysql',
          source_database_label: 'MySQL',
          created_at: '2026-11-01T10:00:00Z',
          progress: { stages, ...(currentStage ? { current_stage: currentStage } : {}) },
        };
      } else if (id === 'get-results') {
        out[id] = synthesis ? { success: true, synthesis } : { success: false };
      } else if (id === 'assignments') {
        out[id] = {
          success: true,
          assignment: {
            query_assignments: [
              ...Array(3).fill({ assigned_engine: 'dynamodb' }),
              ...Array(2).fill({ assigned_engine: 'aurora_mysql' }),
            ],
          },
        };
      } else {
        out[id] = { success: true };
      }
    });
    return out;
  };
}

let container;
let root;
beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});
afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

async function renderPage(scenario) {
  ApiManager.mockImplementation(function MockApi() {
    this.execute = jest.fn(respond(scenario));
  });
  await act(async () => {
    root.render(<JobMonitoringSummary />);
  });
  await act(async () => {});
}

const text = () => container.textContent;
const viewResults = () => Array.from(container.querySelectorAll('a')).find((a) => a.textContent === 'View results');

describe('a finished assessment', () => {
  test('summarizes the database, source, targets, cost and overall risk from the report facts', async () => {
    await renderPage({ stages: COMPLETE_STAGES, synthesis: SYNTHESIS });
    const summary = container.textContent;
    ['Database', 'wordpress', 'Source', 'MySQL', 'Target engines', 'Aurora MySQL', 'DynamoDB',
      'ElastiCache (cache layer)', 'Monthly cost', '$1,234.50/mo', 'Overall risk', 'High'].forEach((part) =>
      expect(summary).toContain(part));
    expect(container.querySelectorAll('[data-engine-badge]')).toHaveLength(3);
    expect(text()).toContain('Assessment id');
    expect(text()).toContain('job-1');
  });

  test('"View results" is the primary button in the page header, next to the title', async () => {
    await renderPage({ stages: COMPLETE_STAGES, synthesis: SYNTHESIS });
    const link = viewResults();
    expect(link.getAttribute('href')).toBe('/analysis/results/job-1');
    expect(link.className).toMatch(/primary/);
    let node = container.querySelector('h1');
    while (node && !node.contains(link)) node = node.parentElement;
    expect(node.textContent).not.toContain('Monthly cost');
  });

  test('shows no phase tracker and no all-ticks list, and no success banner', async () => {
    await renderPage({ stages: COMPLETE_STAGES, synthesis: SYNTHESIS });
    expect(container.querySelector('[aria-label="Assessment phases"]')).toBeNull();
    expect(text()).not.toContain('Assessment complete');
  });

  test('a figure the report does not state is a dash, never $0.00 or a made-up level', async () => {
    await renderPage({ stages: COMPLETE_STAGES, synthesis: { facts: {} } });
    expect(text()).toContain('Monthly cost—');
    expect(text()).toContain('Overall risk—');
    expect(text()).not.toContain('$0.00');
  });

  test('prints the risk assessment\'s overall level, the one the results page prints, not the brief\'s', async () => {
    await renderPage({ stages: COMPLETE_STAGES, synthesis: SYNTHESIS });
    expect(text()).toContain('Overall riskHigh');
    expect(text()).not.toContain('Overall riskLow');
  });

  test('the query flow reads the facts\' final engine counts, not the assignment\'s, and names its root "queries"', async () => {
    // The assignment endpoint answers 3 and 2 queries; the facts say 1,040 and 60.
    await renderPage({ stages: COMPLETE_STAGES, synthesis: SYNTHESIS });
    const img = container.querySelector('svg[role="img"]');
    expect(img.getAttribute('aria-label')).toBe(
      'Query flow of 1,100 queries, split as Aurora MySQL 1,040, DynamoDB 60.',
    );
    expect(Array.from(img.querySelectorAll('text')).map((n) => n.textContent)).toContain('queries (1100)');
    expect(text()).not.toContain('patterns');
  });

  test('"Show as a table" lists the same counts as the facts', async () => {
    await renderPage({ stages: COMPLETE_STAGES, synthesis: SYNTHESIS });
    await act(async () => {
      container.querySelector('[aria-expanded]').click();
    });
    const rows = Array.from(container.querySelectorAll('tbody tr')).map((tr) =>
      Array.from(tr.querySelectorAll('td')).map((td) => td.textContent));
    expect(rows.slice(0, 2)).toEqual([['Aurora MySQL', '1,040'], ['DynamoDB', '60']]);
  });
});

describe('a running assessment', () => {
  test('shows its phases as Steps, the current one in progress, and no summary or button', async () => {
    await renderPage({ stages: RUNNING_STAGES });
    const steps = Array.from(container.querySelectorAll('[aria-label="Assessment phases"] li'));
    expect(steps.map((li) => li.querySelector('[role="img"]')?.getAttribute('aria-label')))
      .toEqual(['Done', 'In progress', 'Not started', 'Not started', 'Not started']);
    expect(text()).toContain('Analyze');
    expect(text()).not.toContain('Analyse');
    expect(text()).not.toContain('Monthly cost');
    expect(viewResults()).toBeUndefined();
  });

  test('the query flow of a running job counts the assignment and states no total', async () => {
    await renderPage({
      stages: [stage('RunCollector'), stage('WaitForAssignmentApproval'), stage('RunSchemaDesign', 'in-progress')],
    });
    expect(container.querySelector('svg[role="img"]').getAttribute('aria-label'))
      .toBe('Query flow, split as DynamoDB 3, Aurora MySQL 2.');
  });

  test('names the assessment id and gives its copy button a name', async () => {
    await renderPage({ stages: RUNNING_STAGES });
    expect(text()).toContain('Assessment id');
    expect(container.querySelector('button[aria-label="Copy assessment id for wordpress (job-1)"]')).not.toBeNull();
  });
});

describe('an assessment waiting at the assignment review', () => {
  test('offers the review as a Cloudscape primary button with no colours of its own (#2311)', async () => {
    await renderPage({
      stages: [stage('RunCollector'), stage('RunRefereeTriage'), stage('WaitForAssignmentApproval', 'in-progress')],
      currentStage: 'WaitForAssignmentApproval',
    });
    expect(text()).toContain('Assessment ready, waiting for your review');
    const review = Array.from(container.querySelectorAll('a')).find((a) => a.textContent === 'Review assignments');
    expect(review.getAttribute('href')).toBe('/analysis/assignments/job-1');
    expect(review.className).toMatch(/primary/);
    expect(review.getAttribute('style')).toBeNull();
  });
});
