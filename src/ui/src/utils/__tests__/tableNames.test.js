// #1536: "No source table" instead of the "unknown" placeholder, with the stored value kept.
import { NO_SOURCE_TABLE, displayGroupName, displayTables } from '../tableNames';

describe('displayTables', () => {
  test('the placeholder reads as no source table', () => {
    expect(displayTables(['unknown'])).toEqual(['No source table']);
    expect(NO_SOURCE_TABLE).toBe('No source table');
  });

  test('real tables are kept, placeholders next to them dropped', () => {
    expect(displayTables(['wp.wp_posts', 'unknown'])).toEqual(['wp_posts']);
  });

  test('short=false keeps the schema prefix', () => {
    expect(displayTables(['wp.wp_posts'], { short: false })).toEqual(['wp.wp_posts']);
  });

  test('the dummy tables of a SELECT with no FROM are placeholders too', () => {
    expect(displayTables(['DUAL'])).toEqual(['No source table']);
    expect(displayTables(['SYSIBM.SYSDUMMY1'])).toEqual(['No source table']);
  });

  test('no tables at all stays empty', () => {
    expect(displayTables([])).toEqual([]);
    expect(displayTables(undefined)).toEqual([]);
  });
});

describe('displayGroupName', () => {
  test('the label a saved report carries reads as the current one', () => {
    expect(displayGroupName('Table not identified by the collector')).toBe('No source table');
  });

  test('other names are unchanged', () => {
    expect(displayGroupName('wp.wp_posts')).toBe('wp.wp_posts');
    expect(displayGroupName('No source table')).toBe('No source table');
  });
});
