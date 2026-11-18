// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0

import {
  RISK_SEVERITIES,
  UNRATED,
  filterRisksWithContent,
  groupRisksBySeverity,
  mitigationRepeatsDescription,
  riskHasContent,
  riskSeverityStatus,
  sharedMitigationRef,
  splitRiskDescription,
} from '../riskAssessment';

describe('splitRiskDescription', () => {
  it('splits the leading [engine] tag off the body', () => {
    expect(splitRiskDescription('[dynamodb] no server-side aggregation')).toEqual({
      engine: 'dynamodb',
      body: 'no server-side aggregation',
    });
  });

  it('defaults to (general) when there is no [engine] tag', () => {
    expect(splitRiskDescription('a general risk')).toEqual({ engine: '(general)', body: 'a general risk' });
    expect(splitRiskDescription('')).toEqual({ engine: '(general)', body: '' });
    expect(splitRiskDescription(undefined)).toEqual({ engine: '(general)', body: '' });
  });
});

describe('riskHasContent / filterRisksWithContent (#201 parity with risks.filtered_risks)', () => {
  it('is false for an empty body or a bare "unknown:" after the engine tag', () => {
    expect(riskHasContent('[dynamodb] unknown:')).toBe(false);
    expect(riskHasContent('[dynamodb]')).toBe(false);
    expect(riskHasContent('')).toBe(false);
  });

  it('is true once there is real text after the tag', () => {
    expect(riskHasContent('[dynamodb] unknown: but with detail')).toBe(true);
    expect(riskHasContent('[dynamodb] a real risk')).toBe(true);
  });

  it('drops contentless risks from the counted/rendered list', () => {
    const risks = [
      { severity: 'HIGH', description: '[dynamodb] a real risk' },
      { severity: 'LOW', description: '[documentdb] unknown:' },
      null,
      { severity: 'MEDIUM' },
    ];
    expect(filterRisksWithContent(risks)).toEqual([{ severity: 'HIGH', description: '[dynamodb] a real risk' }]);
  });
});

describe('riskSeverityStatus (#270/#373: CRITICAL at least as severe as HIGH)', () => {
  it('maps CRITICAL and HIGH to error, never to warning', () => {
    expect(riskSeverityStatus('CRITICAL')).toBe('error');
    expect(riskSeverityStatus('HIGH')).toBe('error');
    expect(riskSeverityStatus('critical')).toBe('error');
  });

  it('maps MEDIUM to warning and LOW to success', () => {
    expect(riskSeverityStatus('MEDIUM')).toBe('warning');
    expect(riskSeverityStatus('LOW')).toBe('success');
  });

  it('maps an unrecognized severity to stopped, not success -- unrated is not the same as low (#270 round-1 review)', () => {
    expect(riskSeverityStatus('')).toBe('stopped');
    expect(riskSeverityStatus(undefined)).toBe('stopped');
    expect(riskSeverityStatus('WEIRD')).toBe('stopped');
  });
});

describe('groupRisksBySeverity', () => {
  it('returns one bucket per severity, including empty ones (so CRITICAL always has a tab)', () => {
    const risks = [
      { severity: 'HIGH', description: '[dynamodb] r1' },
      { severity: 'MEDIUM', description: '[dynamodb] r2' },
    ];
    const groups = groupRisksBySeverity(risks);
    expect(Object.keys(groups)).toEqual(RISK_SEVERITIES);
    expect(groups.CRITICAL).toEqual([]);
    expect(groups.HIGH).toHaveLength(1);
    expect(groups.MEDIUM).toHaveLength(1);
    expect(groups.LOW).toEqual([]);
  });

  it('a CRITICAL risk lands in its own CRITICAL bucket, not folded into HIGH', () => {
    const risks = [{ severity: 'CRITICAL', description: '[dynamodb] no serving engine' }];
    const groups = groupRisksBySeverity(risks);
    expect(groups.CRITICAL).toHaveLength(1);
    expect(groups.HIGH).toHaveLength(0);
  });

  it('a risk with an unrecognized severity gets its own UNRATED bucket, not folded into LOW (#270 round-1 review)', () => {
    const risks = [{ severity: 'WEIRD', description: '[x] y' }];
    const groups = groupRisksBySeverity(risks);
    const total = Object.values(groups).reduce((n, list) => n + list.length, 0);
    expect(total).toBe(1);
    expect(groups.LOW).toEqual([]);
    expect(groups[UNRATED]).toHaveLength(1);
  });

  it('does not add an UNRATED bucket when every risk has a known severity', () => {
    const risks = [{ severity: 'HIGH', description: '[x] y' }];
    const groups = groupRisksBySeverity(risks);
    expect(groups[UNRATED]).toBeUndefined();
  });
});

describe('mitigationRepeatsDescription (mirrors risks._repeats)', () => {
  it('is true when the mitigation text already appears in the description', () => {
    expect(mitigationRepeatsDescription('Keep it on Aurora.', 'Risk text. Keep it on Aurora.')).toBe(true);
  });

  it('is false when the mitigation adds new information', () => {
    expect(mitigationRepeatsDescription('Pre-compute aggregates.', 'Risk text about aggregation.')).toBe(false);
  });

  it('is false for an empty mitigation', () => {
    expect(mitigationRepeatsDescription('', 'anything')).toBe(false);
  });
});

describe('sharedMitigationRef (reads the payload\'s mitigation_refs, #1510)', () => {
  const refs = { 'RISK-004': 'RISK-003' };
  it('names the earlier risk a risk shares its mitigation with', () => {
    expect(sharedMitigationRef(refs, { risk_id: 'RISK-004' })).toBe('RISK-003');
  });
  it('is null for the risk that states it, or with no refs', () => {
    expect(sharedMitigationRef(refs, { risk_id: 'RISK-003' })).toBeNull();
    expect(sharedMitigationRef(undefined, { risk_id: 'RISK-004' })).toBeNull();
    expect(sharedMitigationRef(refs, {})).toBeNull();
  });
});

describe('severity labels and risk titles (#2322)', () => {
  const { riskKey, riskSeverityLabel, riskSeverityLabelKey, riskTitle } = require('../riskAssessment');
  const en = require('../../locales/en.json');
  const t = (key) => en[key];

  it('labels each known severity in sentence case and anything else Unrated', () => {
    expect(RISK_SEVERITIES.map((s) => riskSeverityLabel(s, t))).toEqual(['Critical', 'High', 'Medium', 'Low']);
    expect(riskSeverityLabel('high', t)).toBe('High');
    expect(riskSeverityLabel(UNRATED, t)).toBe('Unrated');
    expect(riskSeverityLabel(undefined, t)).toBe('Unrated');
    expect(riskSeverityLabelKey('x')).toBe('analysis-results.risk-assessment.severity.unrated');
  });

  it('titles a risk with its first sentence, cut at a word when it is long', () => {
    expect(riskTitle({ description: '[dynamodb] First one. Second one.' })).toBe('First one.');
    expect(riskTitle({ description: 'No full stop here' })).toBe('No full stop here');
    expect(riskTitle({ description: '' })).toBe('');
    expect(riskTitle({ description: '1.5x more writes. More.' })).toBe('1.5x more writes.');
    const long = `${'word '.repeat(60)}end.`;
    const title = riskTitle({ description: long });
    expect(title.length).toBeLessThanOrEqual(141);
    expect(title.endsWith('…')).toBe(true);
    expect(title).not.toMatch(/wor…$/);
  });

  it('keys a risk by its id, else by severity and place', () => {
    expect(riskKey('HIGH', { risk_id: 'R-1' }, 3)).toBe('risk-R-1');
    expect(riskKey('HIGH', {}, 3)).toBe('risk-HIGH-3');
  });
});
