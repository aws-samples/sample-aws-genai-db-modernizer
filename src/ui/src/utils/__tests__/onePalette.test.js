/**
 * #1246 / #1088: no page or component keeps an engine palette of its own. Engine badges
 * go through EngineBadge (the catalog colour) and engine colours through engineHex, so
 * a reader sees one colour per engine on every view, and a new catalog engine is
 * coloured everywhere with no edit. A source scan, like rendererOnlyFormat.test.js.
 */
import fs from 'fs';
import path from 'path';

const SRC = path.resolve(__dirname, '../..');

const sources = () => ['pages', 'components'].flatMap((dir) => fs
  .readdirSync(`${SRC}/${dir}`)
  .filter((name) => name.endsWith('.js'))
  .map((name) => ({ name: `${dir}/${name}`, text: fs.readFileSync(`${SRC}/${dir}/${name}`, 'utf8') })));

test('the scan reads the pages that draw engines', () => {
  const names = sources().map((s) => s.name);
  ['pages/AnalysisResults.js', 'pages/AssignmentGate.js', 'pages/PatternAnalysis.js',
    'pages/JobMonitoringSummary.js', 'components/MigrationWavePlan.js', 'components/ChartSankey.js']
    .forEach((name) => expect(names).toContain(name));
});

test('no page or component defines its own engine colour table', () => {
  sources().forEach(({ name, text }) => {
    expect([name, /const\s+ENGINE_(COLORS|HEX|BADGE_COLORS)\s*=\s*\{/.test(text)]).toEqual([name, false]);
    expect([name, /ENGINE_BADGE_COLORS/.test(text)]).toEqual([name, false]);
  });
});

test('no engine badge picks a named Cloudscape colour', () => {
  // A Badge whose colour depends on an engine id, e.g. color={X[engine]} or
  // color={ENGINE_COLORS[e]?.badge}: the four named colours cannot match the palette.
  sources().forEach(({ name, text }) => {
    const engineBadges = text.match(/<Badge[^>]*color=\{[^}]*(engine|\.badge)[^}]*\}/gi) || [];
    expect([name, engineBadges]).toEqual([name, []]);
  });
});

test('the roadmap, the results page, the gate and the pattern page draw engines with EngineBadge', () => {
  const byName = Object.fromEntries(sources().map((s) => [s.name, s.text]));
  ['pages/AnalysisResults.js', 'pages/AssignmentGate.js', 'pages/PatternAnalysis.js', 'components/MigrationWavePlan.js']
    .forEach((name) => expect([name, /<EngineBadge\b/.test(byName[name])]).toEqual([name, true]));
});
