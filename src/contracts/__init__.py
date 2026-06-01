"""
Database Modernizer - Contract Models

Pydantic models for all agent contracts.
"""

from .analysis_input import AnalysisInput
from .analysis_output import AnalysisOutputContract

# Input contracts
from .collector_input import CollectorInput

# Output contracts
from .collector_output import CollectorOutputContract
from .dynamodb_model_output import DynamoDBModelOutputContract

# Schema design contracts
from .dynamodb_pe_review import ChangeRequest, PEReviewResult, ReviewVerdict
from .reality_check_output import RealityCheckOutputContract
from .referee_input import RefereeInput
from .referee_output import RefereeOutputContract
from .schema_design_input import (
    AgentAnalysisInput,
    AgentCollectorInput,
    AgentContextInput,
    project_schema_design_input,
)
from .schema_design_output import SchemaDesignOutputBase
from .synthesis_output import SynthesisOutputContract
from .triage_output import TriageOutputContract

__all__ = [
    # Input contracts
    "CollectorInput",
    "AnalysisInput",
    "RefereeInput",
    # Output contracts
    "CollectorOutputContract",
    "AnalysisOutputContract",
    "RefereeOutputContract",
    # Schema design contracts
    "AgentCollectorInput",
    "AgentAnalysisInput",
    "AgentContextInput",
    "project_schema_design_input",
    "SchemaDesignOutputBase",
    "DynamoDBModelOutputContract",
    "PEReviewResult",
    "ReviewVerdict",
    "ChangeRequest",
    # Reality check contract
    "RealityCheckOutputContract",
    # Triage contract
    "TriageOutputContract",
    # Synthesis contract
    "SynthesisOutputContract",
]

__version__ = "2.0"
