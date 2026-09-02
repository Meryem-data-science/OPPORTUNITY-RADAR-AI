"""Public contracts for the pure Phase 5.2A Portfolio engine."""

from .engine import build_portfolio_assessment, build_portfolio_assessments
from .fingerprint import (
    canonical_portfolio_assessment_payload,
    portfolio_assessment_fingerprint,
)
from .models import (
    PORTFOLIO_ENGINE_VERSION,
    PORTFOLIO_RULES_VERSION,
    PortfolioAssessment,
    PortfolioBucket,
    PortfolioDisposition,
    PortfolioInput,
    PortfolioInputError,
    PortfolioReasonCode,
)
from .input_assembly import (
    PORTFOLIO_INPUT_ASSEMBLY_VERSION,
    PortfolioAssemblyIssue,
    PortfolioAssemblyIssueCode,
    PortfolioAssemblyResult,
    PortfolioAssemblyStatus,
    assemble_portfolio_inputs,
)
from .dry_run import PortfolioDryRunResult, dry_run_portfolio

__all__ = [
    "PORTFOLIO_ENGINE_VERSION",
    "PORTFOLIO_INPUT_ASSEMBLY_VERSION",
    "PORTFOLIO_RULES_VERSION",
    "PortfolioAssessment",
    "PortfolioAssemblyIssue",
    "PortfolioAssemblyIssueCode",
    "PortfolioAssemblyResult",
    "PortfolioAssemblyStatus",
    "PortfolioBucket",
    "PortfolioDisposition",
    "PortfolioInput",
    "PortfolioInputError",
    "PortfolioReasonCode",
    "PortfolioDryRunResult",
    "assemble_portfolio_inputs",
    "build_portfolio_assessment",
    "build_portfolio_assessments",
    "canonical_portfolio_assessment_payload",
    "portfolio_assessment_fingerprint",
    "dry_run_portfolio",
]
