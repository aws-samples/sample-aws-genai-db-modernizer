import { useState, useEffect, useMemo } from 'react';
import ApiManager from '../classes/ApiManager';

/**
 * Fetches analysis data from the real API for a given job ID.
 * 1. Fetch triage → get selected_agents list
 * 2. Fetch collector output
 * 3. Fetch analysis/{agent_type} for each selected agent
 *
 * Returns the same shape as useLocalAnalysis so the UI components are reusable.
 */

export default function useEngineAnalysis(jobId) {
  const [engines, setEngines] = useState({});
  const [collector, setCollector] = useState(null);
  const [triage, setTriage] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!jobId) { setLoading(false); setError('No job ID'); return; }

    (async () => {
      try {
        const api = new ApiManager();

        // Step 1: fetch triage + collector in parallel
        const initial = await api.execute([
          { id: 'triage', path: `assessments/${jobId}/triage`, method: 'GET' },
          { id: 'collector', path: `assessments/${jobId}/collector`, method: 'GET' },
        ]);

        if (initial.triage?.error) throw new Error(`Triage: ${initial.triage.error?.message || 'failed'}`);
        if (initial.collector?.error) throw new Error(`Collector: ${initial.collector.error?.message || 'failed'}`);

        const triageData = initial.triage;
        const collectorData = initial.collector;
        setTriage(triageData);
        setCollector(collectorData);

        // Step 2: determine which engines to fetch from triage selected_agents
        const selectedEngines = (triageData.selected_agents || []).map(a => a.agent_type);
        if (selectedEngines.length === 0) {
          setEngines({});
          setLoading(false);
          return;
        }

        // Step 3: fetch all analysis artifacts in parallel
        const analysisCalls = selectedEngines.map(eng => ({
          id: `analysis-${eng}`,
          path: `assessments/${jobId}/analysis/${eng}`,
          method: 'GET',
        }));

        const analysisResults = await api.execute(analysisCalls);

        const engineMap = {};
        selectedEngines.forEach(eng => {
          const result = analysisResults[`analysis-${eng}`];
          if (result && !result.error) {
            engineMap[eng] = result;
          }
        });

        setEngines(engineMap);
      } catch (err) {
        setError(err.message);
      } finally {
        setLoading(false);
      }
    })();
  }, [jobId]);

  // Reuse the same query-row building logic from useLocalAnalysis
  const queryRows = useMemo(() => {
    if (!collector || Object.keys(engines).length === 0) return [];

    const queries = (collector.queries?.query_patterns || []).filter(
      q => q.query_type !== 'OTHER' && !q.tables_accessed?.includes('unknown')
    );

    const engineQueryMaps = {};
    for (const [engineName, analysis] of Object.entries(engines)) {
      const qMap = {};
      const tableScores = {};
      (analysis.table_recommendations || []).forEach(t => { tableScores[t.table_id] = t; });

      (analysis.workload_analysis?.patterns_detected || []).forEach(pattern => {
        const patternTables = pattern.table_ids || [];
        const avgScore = patternTables.length > 0
          ? patternTables.reduce((sum, tid) => sum + (tableScores[tid]?.confidence_score || 0), 0) / patternTables.length
          : 0;
        (pattern.query_ids || []).forEach(qid => {
          if (!qMap[qid]) qMap[qid] = { scores: [], patterns: [], tables: new Set(), confidence: pattern.confidence };
          qMap[qid].scores.push(avgScore);
          qMap[qid].patterns.push(pattern.pattern_id);
          patternTables.forEach(t => qMap[qid].tables.add(t));
        });
      });

      (analysis.workload_analysis?.anti_patterns_detected || []).forEach(ap => {
        (ap.query_ids || []).forEach(qid => {
          if (!qMap[qid]) qMap[qid] = { scores: [], patterns: [], tables: new Set(), antiPatterns: [] };
          if (!qMap[qid].antiPatterns) qMap[qid].antiPatterns = [];
          qMap[qid].antiPatterns.push(ap.anti_pattern_id);
        });
      });

      for (const qid of Object.keys(qMap)) {
        const entry = qMap[qid];
        entry.score = entry.scores.length > 0
          ? Math.round(entry.scores.reduce((a, b) => a + b, 0) / entry.scores.length) : 0;
        entry.tables = [...entry.tables];
      }
      engineQueryMaps[engineName] = qMap;
    }

    return queries.map(q => {
      const row = {
        queryId: q.query_id, queryText: q.query_text, queryType: q.query_type,
        tablesAccessed: q.tables_accessed || [], frequencyPerHour: q.frequency_per_hour,
        rps: q.calls_per_second, avgLatencyMs: q.execution_time_ms_avg,
        hasJoins: q.has_joins, joinCount: q.join_count,
        fullTableScans: q.full_table_scans, lockTimePct: q.lock_time_pct,
        errors: q.errors, engines: {}, bestEngine: null, bestScore: 0,
      };
      for (const engineName of Object.keys(engines)) {
        const eData = engineQueryMaps[engineName]?.[q.query_id];
        row.engines[engineName] = {
          score: eData?.score || 0, patterns: eData?.patterns || [],
          tables: eData?.tables || [], antiPatterns: eData?.antiPatterns || [],
          confidence: eData?.confidence || null,
        };
        if ((eData?.score || 0) > row.bestScore) { row.bestScore = eData?.score || 0; row.bestEngine = engineName; }
      }
      return row;
    }).sort((a, b) => b.bestScore - a.bestScore);
  }, [collector, engines]);

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

  return { queryRows, engines, engineStats, collector, triage, loading, error };
}
