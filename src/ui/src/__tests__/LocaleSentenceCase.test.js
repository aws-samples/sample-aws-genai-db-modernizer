// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

/**
 * #2328: the UI uses one vocabulary and sentence case (Cloudscape's writing
 * guideline). Headings, labels, buttons, tabs, table headers, breadcrumbs and
 * alert headers in en.json capitalize only their first word and proper nouns.
 * The glossary (assessment, query, access pattern, engine, assessment id) is in
 * docs/ui-specs/README.md, "UI vocabulary".
 */
import fs from 'fs';
import path from 'path';
import en from '../locales/en.json';
import { SideNavigationConfigurations } from '../config/GlobalConfigurations';

// Keys whose text is a heading, label, button, tab, table header, breadcrumb or alert
// header: matched on the key's last segment.
const HEADING_SEGMENT = /(^|-)(title|header|heading|label|button|breadcrumb|tab|crumb)(s)?($|-|_)|^col-|^tab-/;
const HEADING_GROUP = /\.(actions|tabs|breadcrumb|header|on-this-page|steps|figures)\./;

// Names that keep their capitals mid-sentence, each with its reason. Phrases are removed
// before the check; single words are skipped wherever they appear.
const PROPER_PHRASES = {
  'Database Modernizer Assessment Tool': 'the product name (page and navigation title)',
  'Performance Insights': 'AWS feature name',
  'Secrets Manager': 'AWS service name',
  'SQL Server': 'source engine name',
};
const PROPER_NOUNS = {
  AWS: 'company and product prefix',
  Amazon: 'product prefix',
  Aurora: 'engine name, as src/shared/engine_names.py and the catalog spell it',
  DynamoDB: 'engine name, as src/shared/engine_names.py and the catalog spell it',
  DocumentDB: 'engine name, as src/shared/engine_names.py and the catalog spell it',
  ElastiCache: 'engine name, as src/shared/engine_names.py and the catalog spell it',
  OpenSearch: 'engine name, as src/shared/engine_names.py and the catalog spell it',
  Valkey: 'engine name',
  CloudWatch: 'AWS service name',
  MySQL: 'source engine name',
  PostgreSQL: 'source engine name',
  MariaDB: 'source engine name',
  MongoDB: 'compatibility name',
  Oracle: 'source engine name',
  Markdown: 'file format name, as the deliverables list (FOLDER_ENTRIES) spells it',
};
// Whole strings that quote a section's own name.
const KEY_EXCEPTIONS = {
  'analysis-results.adoption.one-step.link-architecture-tab': 'names the "Target engines" section of another tab',
};

// A word after one of these starts a new sentence or clause.
const SENTENCE_BREAK = /[.:?!]$|^[-–·|→]$/;

function titleCaseWords(text) {
  let rest = text.replace(/\{\{[^}]*\}\}/g, '');
  Object.keys(PROPER_PHRASES).forEach((phrase) => { rest = rest.split(phrase).join('name'); });
  const words = rest.split(/\s+/).filter(Boolean);
  const hits = [];
  words.forEach((raw, i) => {
    if (i === 0 || SENTENCE_BREAK.test(words[i - 1])) return;
    const word = raw.replace(/^[("'“]+|[)"'”.,;:!?]+$/g, '');
    if (word === 'ID') { hits.push(word); return; }
    if (/^[A-Z][a-z]/.test(word) && !(word in PROPER_NOUNS)) hits.push(word);
  });
  return hits;
}

function isHeadingKey(key) {
  return HEADING_SEGMENT.test(key.split('.').pop()) || HEADING_GROUP.test(key);
}

const headingEntries = Object.entries(en).filter(([key]) => isHeadingKey(key) && !(key in KEY_EXCEPTIONS));

describe('en.json headings and buttons are sentence case (#2328)', () => {
  test('the guard looks at a real set of heading and button strings', () => {
    expect(headingEntries.length).toBeGreaterThan(300);
    expect(headingEntries.map(([key]) => key)).toEqual(expect.arrayContaining([
      'dashboard.header.title',
      'dashboard.header.new-analysis',
      'analysis-results.tabs.queries',
      'assignment-gate.breadcrumb.assignment-review',
      'dashboard.table.col-job-id',
    ]));
  });

  test('no heading or button capitalizes a word other than the first or a proper noun', () => {
    const offenders = headingEntries
      .map(([key, value]) => [key, value, titleCaseWords(value)])
      .filter(([, , hits]) => hits.length > 0)
      .map(([key, value, hits]) => `${key} = "${value}" (${hits.join(', ')})`);
    expect(offenders).toEqual([]);
  });

  test('the side navigation is sentence case too', () => {
    SideNavigationConfigurations.items.forEach((item) => {
      expect(titleCaseWords(item.text)).toEqual([]);
    });
  });

  test('the check catches Title Case and passes proper nouns', () => {
    expect(titleCaseWords('Executive Summary')).toEqual(['Summary']);
    expect(titleCaseWords('Invalid Job ID')).toEqual(['Job', 'ID']);
    expect(titleCaseWords('Cache with ElastiCache')).toEqual([]);
    expect(titleCaseWords('Wave {{wave}}: Ready')).toEqual([]);
    expect(titleCaseWords('{{count}} queries on Aurora')).toEqual([]);
    expect(titleCaseWords('Page not found - Database Modernizer Assessment Tool')).toEqual([]);
    expect(titleCaseWords('Delete Assessment artifacts')).toEqual(['Assessment']);
  });
});

describe('one vocabulary across the UI (#2328)', () => {
  const visible = Object.entries(en);

  test('no string calls an assessment an analysis or a job', () => {
    // "analysis" the activity ("workload analysis", "analysis agents") stays.
    const offenders = visible
      .filter(([, value]) => /\b(analyses|jobs?|job's)\b|\b(an|this|the|new|cancel) analysis\b/i.test(value))
      .map(([key, value]) => `${key} = "${value}"`);
    expect(offenders).toEqual([]);
  });

  test('"Assessments" is the one breadcrumb root and side-navigation entry', () => {
    expect(en['common.breadcrumb.assessments']).toBe('Assessments');
    const texts = SideNavigationConfigurations.items.map((item) => item.text);
    expect(texts).toEqual(['Assessments', 'New assessment']);
    expect(Object.keys(en).filter((key) => /\.breadcrumb\.(home|dashboard|analyses)$/.test(key))).toEqual([]);
  });

  test('every page breadcrumb starts at Assessments, never Home or Dashboard', () => {
    const read = (file) => fs.readFileSync(file, 'utf8');
    const pages = {
      Dashboard: read(path.join(__dirname, '../pages/Dashboard.js')),
      CreateAnalysis: read(path.join(__dirname, '../pages/CreateAnalysis.js')),
      JobMonitoringSummary: read(path.join(__dirname, '../pages/JobMonitoringSummary.js')),
      AssignmentGate: read(path.join(__dirname, '../pages/AssignmentGate.js')),
      GraphExplorer: read(path.join(__dirname, '../pages/GraphExplorer.js')),
      AssessmentNotFound: read(path.join(__dirname, '../components/AssessmentNotFound.js')),
      ResultsPageHeader: read(path.join(__dirname, '../components/ResultsPageHeader.js')),
    };
    Object.entries(pages).forEach(([file, source]) => {
      expect([file, source.includes("common.breadcrumb.assessments")]).toEqual([file, true]);
      expect([file, /breadcrumb\.(home|dashboard|analyses)\b|text: `Job /.test(source)]).toEqual([file, false]);
    });
  });

  test('the assessment id is spelled "Assessment id"', () => {
    expect(en['dashboard.table.col-job-id']).toBe('Assessment id');
    expect(en['job-summary.progress.job-id-label']).toBe('Assessment id');
    expect(en['analysis-results.header.assessment-id']).toBe('Assessment id');
  });

  test('query counts say queries, not patterns', () => {
    expect(en['job-summary.collector.query-patterns']).toBe('Queries');
    expect(en['assignment-gate.summary.queries-count_other']).toBe('{{count}} queries');
    ['job-summary', 'analysis-results', 'assignment-gate'].forEach((page) => {
      expect(en[`${page}.query-flow.description`]).toBe('Query distribution across the recommended engines');
    });
  });
});
