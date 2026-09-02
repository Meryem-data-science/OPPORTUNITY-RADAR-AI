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
from .persistence import (
    PortfolioPersistenceError,
    PortfolioStoreResult,
    store_portfolio_batch,
)
from .persistence_fingerprint import (
    PORTFOLIO_PERSISTENCE_VERSION,
    canonical_portfolio_run_payload,
    portfolio_run_fingerprint,
)
from .sync import PortfolioSyncError, PortfolioSyncResult, sync_portfolio

__all__ = [
    "PORTFOLIO_ENGINE_VERSION",
    "PORTFOLIO_INPUT_ASSEMBLY_VERSION",
    "PORTFOLIO_RULES_VERSION",
    "PORTFOLIO_PERSISTENCE_VERSION",
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
    "PortfolioPersistenceError",
    "PortfolioStoreResult",
    "PortfolioSyncError",
    "PortfolioSyncResult",
    "assemble_portfolio_inputs",
    "build_portfolio_assessment",
    "build_portfolio_assessments",
    "canonical_portfolio_assessment_payload",
    "canonical_portfolio_run_payload",
    "portfolio_assessment_fingerprint",
    "dry_run_portfolio",
    "portfolio_run_fingerprint",
    "store_portfolio_batch",
    "sync_portfolio",
]
