# ADR-007: Referee Agent Orchestration

**Status:** Superseded by [ADR-016](ADR-016-compute-and-orchestration-strategy.md)
**Date:** 2026-02-02
**Deciders:** Architecture Team
**Related ADRs:** ADR-001 (State Management), ADR-002 (Pydantic Output), ADR-006 (Analysis Agents)

---

> **Superseded:** The four-phase Referee Agent model described here has been replaced by a Triage/Synthesis split architecture. See [ADR-016](ADR-016-compute-and-orchestration-strategy.md) for the current orchestration strategy.

## Context

The Referee Agent is the final stage in the modernization workflow. It receives outputs from all analysis agents and produces the final modernization report. The Referee must:

- **Validate** analysis outputs for consistency and completeness
- **Cross-check** recommendations across analysis agents (detect conflicts)
- **Prioritize** recommendations by impact, confidence, and effort
- **Generate** final modernization report with executive summary
- **Calculate** TCO (Total Cost of Ownership) estimates
- **Assess** migration risks and complexity

### Requirements

- **Input validation**: Ensure all analysis outputs are complete and valid
- **Conflict detection**: Identify contradictory recommendations
- **Prioritization**: Rank recommendations by value (impact × confidence / effort)
- **Report generation**: Create comprehensive, actionable report
- **Extensible**: Support new analysis types without code changes
- **Resumable**: Checkpoint validation and report generation phases

---

## Decision

We will implement **Referee as a Multi-Phase Orchestrator**:

1. **Phase 1: Validation** - Validate all analysis outputs
2. **Phase 2: Cross-Analysis** - Detect conflicts and dependencies
3. **Phase 3: Prioritization** - Rank recommendations
4. **Phase 4: Report Generation** - Create final report

The Referee is **not** a category like Collector or Analysis. It's a single orchestrator agent that coordinates validation and report generation.

---

## Architecture

### Referee Workflow

```
Analysis Outputs (from all analysis agents)
    ↓ (Already Pydantic-validated by analysis agents)
┌─────────────────────────────────────────────────────────┐
│              Referee Agent Orchestrator                 │
└─────────────────────────────────────────────────────────┘
    ↓
┌─────────────────────────────────────────────────────────┐
│ Phase 1: Completeness Check                             │
│ - Check for missing analysis outputs                    │
│ - Verify all required analyses completed                │
│ - Flag incomplete data (missing tables, patterns, etc.) │
│ - NOT re-validating Pydantic (already done)             │
│ Checkpoint: completeness_check_complete                 │
└─────────────────────────────────────────────────────────┘
    ↓
┌─────────────────────────────────────────────────────────┐
│ Phase 2: Cross-Analysis                                 │
│ - Detect conflicting recommendations                    │
│ - Identify dependencies between recommendations         │
│ - Flag inconsistencies across analyses                  │
│ Checkpoint: cross_analysis_complete                     │
└─────────────────────────────────────────────────────────┘
    ↓
┌─────────────────────────────────────────────────────────┐
│ Phase 3: Prioritization                                 │
│ - Calculate priority scores (formula TBD)               │
│ - Rank recommendations (high/medium/low)                │
│ - Group by category (quick wins, strategic, long-term)  │
│ Note: Formula is a guesstimate, can be refined          │
│ Checkpoint: prioritization_complete                     │
└─────────────────────────────────────────────────────────┘
    ↓
┌─────────────────────────────────────────────────────────┐
│ Phase 4: Report Generation                              │
│ - Generate executive summary                            │
│ - Create detailed recommendation sections               │
│ - Calculate TCO estimates                               │
│ - Assess migration risks                                │
│ Checkpoint: report_complete                             │
└─────────────────────────────────────────────────────────┘
    ↓
ModernizationReport (Pydantic model)
```

---

## Implementation

### Referee Orchestrator

```python
# src/agents/referee/referee_orchestrator.py

from strands import Agent
from pydantic import BaseModel
from typing import Dict, List, Any
from contracts.models.analysis_output import AnalysisOutput
from contracts.models.modernization_report import ModernizationReport

class RefereeOrchestrator:
    """
    Orchestrates referee workflow: validation, cross-analysis, prioritization, report generation.
    """

    def __init__(self, job_id: str):
        self.job_id = job_id

    async def generate_report(
        self,
        collector_output: CollectorOutput,
        analysis_output: AnalysisOutput
    ) -> ModernizationReport:
        """
        Generate final modernization report from analysis outputs.

        Args:
            collector_output: Output from collector agent
            analysis_output: Output from analysis orchestrator (all analyses)

        Returns:
            ModernizationReport (Pydantic model)
        """
        # Phase 1: Completeness Check
        completeness_result = await self._run_phase(
            "completeness_check",
            self._check_completeness,
            analysis_output
        )

        # Phase 2: Cross-Analysis
        cross_analysis = await self._run_phase(
            "cross_analysis",
            self._cross_analyze,
            analysis_output
        )

        # Phase 3: Prioritization
        prioritized = await self._run_phase(
            "prioritization",
            self._prioritize_recommendations,
            analysis_output,
            cross_analysis
        )

        # Phase 4: Report Generation
        report = await self._run_phase(
            "report_generation",
            self._generate_report,
            collector_output,
            analysis_output,
            prioritized
        )

        # Final checkpoint
        save_checkpoint(self.job_id, "referee_complete", report)

        return report

    async def _run_phase(
        self,
        phase_name: str,
        phase_func: callable,
        *args
    ) -> Any:
        """Run a single referee phase with checkpoint"""
        checkpoint_key = f"referee_{phase_name}"

        # Check for existing checkpoint
        checkpoint = load_checkpoint(self.job_id, checkpoint_key)
        if checkpoint:
            logger.info(f"Job {self.job_id}: Referee {phase_name} resumed from checkpoint")
            return checkpoint

        # Run phase
        logger.info(f"Job {self.job_id}: Starting referee {phase_name}")
        result = await phase_func(*args)

        # Checkpoint
        save_checkpoint(self.job_id, checkpoint_key, result)

        # Publish progress
        publish_progress(self.job_id, checkpoint_key, "completed")

        return result

    async def _check_completeness(self, analysis_output: AnalysisOutput) -> Dict:
        """
        Phase 1: Check completeness of analysis outputs.

        NOT re-validating Pydantic (already done by analysis agents).
        Checking for:
        - Missing analysis outputs (expected vs received)
        - Incomplete data (empty lists, missing tables, etc.)
        - Data quality issues (e.g., 0 recommendations when expected)

        Returns completeness check results with warnings.
        """
        agent = Agent(
            system_prompt=self._create_completeness_prompt(),
            response_format=CompletenessResult
        )

        return agent(analysis_output)

    async def _cross_analyze(self, analysis_output: AnalysisOutput) -> Dict:
        """
        Phase 2: Cross-analyze recommendations for conflicts and dependencies.
        """
        agent = Agent(
            system_prompt=self._create_cross_analysis_prompt(),
            response_format=CrossAnalysisResult
        )

        return agent(analysis_output)

    async def _prioritize_recommendations(
        self,
        analysis_output: AnalysisOutput,
        cross_analysis: Dict
    ) -> Dict:
        """
        Phase 3: Prioritize recommendations.
        """
        agent = Agent(
            system_prompt=self._create_prioritization_prompt(),
            response_format=PrioritizationResult
        )

        return agent({
            "analysis_output": analysis_output,
            "cross_analysis": cross_analysis
        })

    async def _generate_report(
        self,
        collector_output: CollectorOutput,
        analysis_output: AnalysisOutput,
        prioritized: Dict
    ) -> ModernizationReport:
        """
        Phase 4: Generate final modernization report.
        """
        agent = Agent(
            system_prompt=self._create_report_generation_prompt(),
            response_format=ModernizationReport
        )

        return agent({
            "collector_output": collector_output,
            "analysis_output": analysis_output,
            "prioritized": prioritized
        })

    def _create_completeness_prompt(self) -> str:
        return """You are a Completeness Check Agent (Referee Phase 1).

Your mission: Check if analysis outputs are complete and sufficient.

IMPORTANT: Do NOT re-validate Pydantic models (already validated by analysis agents).

Completeness checks:
1. Expected analyses present (schema, performance, target-specific, etc.)
2. Data completeness (not empty lists when data expected)
3. Sufficient recommendations (at least 1 per analysis)
4. Key fields populated (not null/empty when required)

Examples of incompleteness:
- Schema analysis returned but 0 tables found (database has tables)
- Performance analysis returned but 0 patterns detected (queries exist)
- Recommendations list is empty (should have at least 1)
- Missing expected analysis type (e.g., Aurora analysis missing for Aurora target)

For each issue:
- Flag as warning (not error - Pydantic already validated structure)
- Explain what's missing or incomplete
- Suggest if analysis should be re-run

Output: CompletenessResult (Pydantic model)
- status: "complete" or "incomplete"
- warnings: List of completeness warnings
- missing_analyses: List of expected but missing analysis types
"""

    def _create_cross_analysis_prompt(self) -> str:
        return """You are a Cross-Analysis Agent (Referee Phase 2).

Your mission: Detect conflicts and dependencies across analysis outputs.

Cross-analysis checks:
1. Conflicting recommendations (e.g., "add index" vs "remove index" on same column)
2. Dependencies (e.g., recommendation B requires recommendation A first)
3. Inconsistencies (e.g., schema analysis says 100 tables, performance analysis says 200)
4. Overlapping recommendations (same recommendation from multiple analyses)

For each conflict:
- Identify conflicting recommendations
- Explain the conflict
- Suggest resolution

For each dependency:
- Identify dependent recommendations
- Explain dependency
- Suggest execution order

Output: CrossAnalysisResult (Pydantic model)
- conflicts: List of conflicts with resolutions
- dependencies: List of dependencies with order
- overlaps: List of overlapping recommendations (merge candidates)
"""

    def _create_prioritization_prompt(self) -> str:
        return """You are a Prioritization Agent (Referee Phase 3).

Your mission: Rank recommendations by value and feasibility.

Prioritization approach (GUESSTIMATE - can be refined later):
Consider:
- Impact: high/medium/low (from analysis agents)
- Confidence: 0.0-1.0 (from analysis agents)
- Effort: low/medium/high (from analysis agents)

Priority tiers:
- High Priority: High impact + high confidence + low effort (quick wins)
- Medium Priority: High impact + medium effort OR medium impact + low effort (strategic)
- Low Priority: Low impact OR high effort (long-term)

Note: Exact formula TBD - this is a starting point for prioritization.
Effort estimation is inherently uncertain (guesstimate).

Grouping:
- Quick Wins: High impact, low effort, high confidence
- Strategic: High impact, medium/high effort
- Long-term: Medium/low impact, any effort

For each recommendation:
- Assign priority tier (high/medium/low)
- Assign group (quick wins/strategic/long-term)
- Consider dependencies (don't prioritize dependent recs before prerequisites)
- Add reasoning for priority assignment

Output: PrioritizationResult (Pydantic model)
- recommendations: List of recommendations with priority assignments
- quick_wins: List of quick win recommendations
- strategic: List of strategic recommendations
- long_term: List of long-term recommendations
"""

    def _create_report_generation_prompt(self) -> str:
        return """You are a Report Generation Agent (Referee Phase 4).

Your mission: Create comprehensive modernization report.

Report structure:
1. Executive Summary
   - Database overview (size, complexity)
   - Key findings (top 3-5 insights)
   - Recommended approach (quick wins first, then strategic)
   - Estimated timeline and effort

2. Quick Wins (High Priority)
   - List of quick win recommendations
   - Expected impact and effort
   - Implementation steps

3. Strategic Improvements (Medium Priority)
   - List of strategic recommendations
   - Expected impact and effort
   - Dependencies and prerequisites

4. Long-Term Optimizations (Low Priority)
   - List of long-term recommendations
   - Expected impact and effort

5. TCO Analysis
   - Current cost estimate (source database)
   - Target cost estimate (modernized)
   - Cost savings (monthly, annual)
   - ROI timeline

6. Risk Assessment
   - Migration complexity (low/medium/high)
   - Key risks and mitigations
   - Recommended migration approach

7. Appendix
   - Detailed analysis outputs
   - Schema diagrams
   - Query patterns

Output: ModernizationReport (Pydantic model)
"""
```

---

## Output Contract

### ModernizationReport (Final Output)

```python
# src/contracts/models/modernization_report.py

from pydantic import BaseModel, Field
from typing import List, Dict, Optional
from datetime import datetime

class Recommendation(BaseModel):
    """Single recommendation with priority"""
    recommendation_id: str
    title: str
    description: str
    category: str
    priority_tier: str = Field(description="high, medium, low")
    group: str = Field(description="quick_wins, strategic, long_term")
    estimated_impact: str
    effort: str
    confidence_score: float
    priority_reasoning: str = Field(description="Why this priority was assigned")
    implementation_steps: List[str]
    dependencies: List[str] = Field(default_factory=list)

class ExecutiveSummary(BaseModel):
    """Executive summary section"""
    database_overview: str
    key_findings: List[str]
    recommended_approach: str
    estimated_timeline: str
    total_recommendations: int
    quick_wins_count: int

class TCOAnalysis(BaseModel):
    """Total Cost of Ownership analysis"""
    current_monthly_cost: float
    target_monthly_cost: float
    monthly_savings: float
    annual_savings: float
    roi_months: int

class RiskAssessment(BaseModel):
    """Migration risk assessment"""
    complexity: str = Field(description="low, medium, high")
    key_risks: List[str]
    mitigations: List[str]
    recommended_approach: str

class ModernizationReport(BaseModel):
    """
    Final modernization report.

    This is the ultimate output of the Database Modernizer workflow.
    """
    job_id: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)

    # Report sections
    executive_summary: ExecutiveSummary
    quick_wins: List[Recommendation]
    strategic: List[Recommendation]
    long_term: List[Recommendation]
    tco_analysis: TCOAnalysis
    risk_assessment: RiskAssessment

    # Metadata
    source_database: Dict = Field(description="Source database metadata")
    target_platform: str = Field(description="Target platform (Aurora, RDS, etc.)")
    analysis_version: str = Field(default="1.0")
```

---

## Validation & Conflict Detection

### Completeness Check (Not Pydantic Re-Validation)

**Key Point:** Analysis agents already return Pydantic-validated outputs. Phase 1 checks for **completeness and sufficiency**, not structural validation.

```python
class CompletenessResult(BaseModel):
    """Output from completeness check phase"""
    status: str = Field(description="complete or incomplete")
    warnings: List[str] = Field(default_factory=list, description="Completeness warnings")
    missing_analyses: List[str] = Field(
        default_factory=list,
        description="Expected but missing analysis types"
    )

    # Per-analysis completeness
    analysis_completeness: Dict[str, Dict] = Field(
        description="Completeness check per analysis type"
    )
```

**Examples of completeness issues:**

- Schema analysis returned 0 tables (but database has tables)
- Performance analysis returned 0 patterns (but queries exist)
- Recommendations list is empty (expected at least 1)
- Missing expected analysis (e.g., Aurora analysis for Aurora target)

### Cross-Analysis Rules

```python
class Conflict(BaseModel):
    """A conflict between recommendations"""
    conflict_id: str
    recommendation_ids: List[str]
    description: str
    resolution: str

class Dependency(BaseModel):
    """A dependency between recommendations"""
    dependent_id: str
    prerequisite_id: str
    description: str

class CrossAnalysisResult(BaseModel):
    """Output from cross-analysis phase"""
    conflicts: List[Conflict]
    dependencies: List[Dependency]
    overlaps: List[Dict] = Field(description="Overlapping recommendations to merge")
```

---

## Prioritization Algorithm

### Priority Assignment (Guesstimate Approach)

**Important:** Prioritization is inherently uncertain. The approach below is a **starting point** that can be refined based on real-world usage.

```python
def assign_priority_tier(
    impact: str,
    confidence: float,
    effort: str
) -> str:
    """
    Assign priority tier for a recommendation.

    This is a GUESSTIMATE approach - exact formula TBD.
    Effort estimation is inherently uncertain.

    Args:
        impact: "high", "medium", or "low"
        confidence: 0.0-1.0
        effort: "low", "medium", or "high"

    Returns:
        Priority tier: "high", "medium", or "low"
    """
    # Quick wins: High impact + high confidence + low effort
    if impact == "high" and confidence >= 0.7 and effort == "low":
        return "high"

    # Strategic: High impact + medium effort OR medium impact + low effort
    if (impact == "high" and effort == "medium") or \
       (impact == "medium" and effort == "low" and confidence >= 0.6):
        return "medium"

    # Long-term: Everything else
    return "low"

# Note: This is a simplified heuristic.
# Real prioritization may consider:
# - Business value
# - Technical dependencies
# - Team capacity
# - Risk tolerance
# - Timeline constraints
```

**Why guesstimate?**

- Effort estimation is inherently uncertain (depends on team, context, etc.)
- Impact is subjective (business value varies)
- Formula can be refined based on feedback
- Starting simple, iterate based on real usage

---

## Consequences

### Positive

✅ **Completeness check**: Catches missing/incomplete data (not duplicate Pydantic validation)
✅ **Conflict detection**: Prevents contradictory recommendations
✅ **Prioritization**: Clear ranking (guesstimate, can be refined)
✅ **Resumable**: Checkpoint per phase
✅ **Extensible**: Supports new analysis types automatically
✅ **Actionable**: Report organized by priority

### Negative

⚠️ **Sequential phases**: Can't parallelize completeness check and report generation
⚠️ **Complexity**: 4 phases to maintain
⚠️ **Priority uncertainty**: Effort estimation is inherently uncertain (guesstimate)

### Neutral

🔶 **Single agent**: Referee is not a category (unlike Collector/Analysis)
🔶 **Priority approach**: Heuristic-based, can be refined with feedback
🔶 **Completeness vs validation**: Phase 1 checks sufficiency, not structure (Pydantic already did that)

---

## Alternatives Considered

### Alternative 1: Single-Phase Referee (Rejected)

**Rejected because:**

- ❌ No intermediate checkpoints
- ❌ Hard to debug failures
- ❌ Can't resume from validation if report generation fails

### Alternative 2: Referee as Multi-Agent Category (Rejected)

**Rejected because:**

- ❌ Referee has clear sequential phases (not parallel)
- ❌ Validation → Cross-Analysis → Prioritization → Report (dependencies)
- ❌ Unnecessary complexity for single orchestrator

### Alternative 3: No Cross-Analysis Phase (Rejected)

**Rejected because:**

- ❌ Conflicts between analyses would go undetected
- ❌ Users would receive contradictory recommendations
- ❌ No dependency tracking

---

## Related Documents

- [ADR-001: State Management and Checkpoints](ADR-001-state-management-and-checkpoints.md)
- [ADR-002: Structured Output with Pydantic](ADR-002-structured-output-and-validation.md)
- [ADR-006: Analysis Agent Architecture](ADR-006-analysis-agent-patterns.md)

---

## Revision History

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0 | 2026-02-02 | Architecture Team | Initial decision |

---

**Status: Accepted and Ready for Implementation**
