import {
  costBaselineStats,
  formatCurrentCostStat,
  formatSavingsPercent,
  isCostBaselineUnknown,
} from '../tcoAnalysis';

describe('isCostBaselineUnknown (#334)', () => {
  it('is unknown when current_cost_known is explicitly false', () => {
    expect(isCostBaselineUnknown({ current_cost_known: false, current_monthly_cost: 500 })).toBe(true);
  });

  it('is known when current_cost_known is true, even if the cost happens to be 0', () => {
    expect(isCostBaselineUnknown({ current_cost_known: true, current_monthly_cost: 0 })).toBe(false);
  });

  it('a real baseline with the field present is known', () => {
    expect(isCostBaselineUnknown({ current_cost_known: true, current_monthly_cost: 500 })).toBe(false);
  });

  it('an older report with no current_cost_known field and a real nonzero cost is known', () => {
    expect(isCostBaselineUnknown({ current_monthly_cost: 500 })).toBe(false);
  });

  it('an older report with no current_cost_known field and a 0 cost is treated as unknown', () => {
    // This is the real wordpress sample evidence shape (#334): no
    // current_cost_known at all, current_monthly_cost: 0.0 -- the field
    // predates #380, and that 0 is the "no RDS instance metadata" placeholder,
    // not a genuine $0 database.
    expect(isCostBaselineUnknown({ current_monthly_cost: 0.0, projected_monthly_cost: 823.72, savings_percent: 0.0 })).toBe(true);
  });

  it('treats an explicit current_cost_known: null the same as a missing field', () => {
    expect(isCostBaselineUnknown({ current_cost_known: null, current_monthly_cost: 0 })).toBe(true);
    expect(isCostBaselineUnknown({ current_cost_known: null, current_monthly_cost: 500 })).toBe(false);
  });

  it('no tco_analysis at all is unknown', () => {
    expect(isCostBaselineUnknown(null)).toBe(true);
    expect(isCostBaselineUnknown(undefined)).toBe(true);
  });
});

describe('formatSavingsPercent', () => {
  it('drops trailing zeros, like renderers.fmt_num', () => {
    expect(formatSavingsPercent(44)).toBe('44%');
    expect(formatSavingsPercent(44.0)).toBe('44%');
    expect(formatSavingsPercent(44.3)).toBe('44.3%');
    expect(formatSavingsPercent(0)).toBe('0%');
  });

  it('is "0%" for a non-numeric value instead of throwing or showing NaN', () => {
    expect(formatSavingsPercent('not-a-number')).toBe('0%');
    expect(formatSavingsPercent(undefined)).toBe('0%');
  });
});

describe('formatCurrentCostStat', () => {
  it('formats a known baseline as a monthly dollar figure', () => {
    expect(formatCurrentCostStat(500)).toBe('$500.00/mo');
    expect(formatCurrentCostStat(0)).toBe('$0.00/mo');
  });
});

describe('costBaselineStats', () => {
  it('returns the formatted current cost and savings when the baseline is known', () => {
    expect(costBaselineStats({ current_cost_known: true, current_monthly_cost: 500, savings_percent: 44 })).toEqual({
      known: true,
      currentCostStat: '$500.00/mo',
      savingsStat: '44%',
    });
  });

  it('returns nulls (not "$0.00/mo"/"0%") when the baseline is unknown', () => {
    expect(costBaselineStats({ current_cost_known: false, current_monthly_cost: 0, savings_percent: 0 })).toEqual({
      known: false,
      currentCostStat: null,
      savingsStat: null,
    });
  });

  it('treats the real wordpress sample evidence (0 cost, no current_cost_known field) as unknown', () => {
    expect(costBaselineStats({ current_monthly_cost: 0.0, projected_monthly_cost: 823.72, savings_percent: 0.0 })).toEqual({
      known: false,
      currentCostStat: null,
      savingsStat: null,
    });
  });
});
