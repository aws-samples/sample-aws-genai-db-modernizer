import { useState, useEffect, useMemo } from 'react';

/**
 * Loads analysis data from local JSON files and builds a cross-engine
 * query-level comparison model.
 *
 * For each source query we produce a row with per-engine scores derived from
 * the workload_analysis.patterns_detected arrays in each engine's analysis.json.
 *
 * Score per query per engine =
 *   avg(confidence_score) of all table_recommendations whose table_id appears
 *   in the same pattern that references this query_id.
 */

const ENGINE_FILES = [
  { engine: 'dynamodb',    file: '/data/analysis-dynamodb.json' },
  { engine: 'documentdb',  file: '/data/analysis-documentdb.json' },
  { engine: 'opensearch',  file: '/data/analysis-opensearch.json' },
];

async function fetchJson(path) {
  const res = await fetch(path);
  if (!res.ok) return null;
  return res.json();
}

export default function useLocalAnalysis() {
  const [engines, setEngines] = useState({});
  const [collector, setCollector] = useState(null);
  const [triage, setTriage] = useState(null);
  const [synthesis, setSynthesis] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    (async () => {
      try {
        const [collectorData, triageData, synthesisData, ...engineResults] = await Promise.all([
          fetchJson('/data/collector-output.json'),
          fetchJson('/data/triage.json'),
          fetchJson('/data/synthesis-report.json'),
          ...ENGINE_FILES.map(e => fetchJson(e.file)),
        ]);
        setCollector(collectorData);
        setTriage(triageData);
        setSynthesis(synthesisData);

        const engineMap = {};
        ENGINE_FILES.forEach((e, i) => {
          if (engineResults[i]) engineMap[e.engine] = engineResults[i];
        });
        setEngines(engineMap);
      } catch (err) {
        setError(err.message);
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  // Build query-level cross-engine rows
  const queryRows = useMemo(() => {
    if (!collector || Object.keys(engines).length === 0) return [];

    const queries = (collector.queries?.query_patterns || []).filter(
      q => q.query_type !== 'OTHER' && !q.tables_accessed?.includes('unknown')
    );

    // For each engine, build: queryId -> { score, patterns[], tables[] }
    const engineQueryMaps = {};
    for (const [engineName, analysis] of Object.entries(engines)) {
      const qMap = {};
      const tableScores = {};
      (analysis.table_recommendations || []).forEach(t => {
        tableScores[t.table_id] = t;
      });

      (analysis.workload_analysis?.patterns_detected || []).forEach(pattern => {
        const patternTables = pattern.table_ids || [];
        const avgScore = patternTables.length > 0
          ? patternTables.reduce((sum, tid) => sum + (tableScores[tid]?.confidence_score || 0), 0) / patternTables.length
          : 0;

        (pattern.query_ids || []).forEach(qid => {
          if (!qMap[qid]) {
            qMap[qid] = { scores: [], patterns: [], tables: new Set(), confidence: pattern.confidence };
          }
          qMap[qid].scores.push(avgScore);
          qMap[qid].patterns.push(pattern.pattern_id);
          patternTables.forEach(t => qMap[qid].tables.add(t));
        });
      });

      // Also check anti-patterns
      (analysis.workload_analysis?.anti_patterns_detected || []).forEach(ap => {
        (ap.query_ids || []).forEach(qid => {
          if (!qMap[qid]) {
            qMap[qid] = { scores: [], patterns: [], tables: new Set(), antiPatterns: [] };
          }
          if (!qMap[qid].antiPatterns) qMap[qid].antiPatterns = [];
          qMap[qid].antiPatterns.push(ap.anti_pattern_id);
        });
      });

      // Compute final score per query
      for (const qid of Object.keys(qMap)) {
        const entry = qMap[qid];
        entry.score = entry.scores.length > 0
          ? Math.round(entry.scores.reduce((a, b) => a + b, 0) / entry.scores.length)
          : 0;
        entry.tables = [...entry.tables];
      }
      engineQueryMaps[engineName] = qMap;
    }

    // Build rows
    return queries.map(q => {
      const row = {
        queryId: q.query_id,
        queryText: q.query_text,
        queryType: q.query_type,
        tablesAccessed: q.tables_accessed || [],
        frequencyPerHour: q.frequency_per_hour,
        rps: q.calls_per_second,
        avgLatencyMs: q.execution_time_ms_avg,
        hasJoins: q.has_joins,
        joinCount: q.join_count,
        fullTableScans: q.full_table_scans,
        lockTimePct: q.lock_time_pct,
        errors: q.errors,
        engines: {},
        bestEngine: null,
        bestScore: 0,
      };

      for (const engineName of Object.keys(engines)) {
        const eData = engineQueryMaps[engineName]?.[q.query_id];
        row.engines[engineName] = {
          score: eData?.score || 0,
          patterns: eData?.patterns || [],
          tables: eData?.tables || [],
          antiPatterns: eData?.antiPatterns || [],
          confidence: eData?.confidence || null,
        };
        if ((eData?.score || 0) > row.bestScore) {
          row.bestScore = eData?.score || 0;
          row.bestEngine = engineName;
        }
      }

      return row;
    }).sort((a, b) => b.bestScore - a.bestScore);
  }, [collector, engines]);

  // Aggregate stats per engine
  const engineStats = useMemo(() => {
    const stats = {};
    for (const [engineName, analysis] of Object.entries(engines)) {
      const recs = analysis.table_recommendations || [];
      const scores = recs.map(r => r.confidence_score);
      stats[engineName] = {
        tableCount: recs.length,
        avgConfidence: scores.length > 0 ? Math.round(scores.reduce((a, b) => a + b, 0) / scores.length) : 0,
        monthlyCost: analysis.cost_estimate?.monthly_cost_usd || 0,
        patternsDetected: analysis.workload_analysis?.patterns_detected?.length || 0,
        antiPatterns: analysis.workload_analysis?.anti_patterns_detected?.length || 0,
        queriesCovered: new Set(
          (analysis.workload_analysis?.patterns_detected || []).flatMap(p => p.query_ids || [])
        ).size,
      };
    }
    return stats;
  }, [engines]);

  return { queryRows, engines, engineStats, collector, triage, synthesis, loading, error };
}
