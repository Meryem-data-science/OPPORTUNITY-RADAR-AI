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

__all__ = [
    "PORTFOLIO_ENGINE_VERSION",
    "PORTFOLIO_RULES_VERSION",
    "PortfolioAssessment",
    "PortfolioBucket",
    "PortfolioDisposition",
    "PortfolioInput",
    "PortfolioInputError",
    "PortfolioReasonCode",
    "build_portfolio_assessment",
    "build_portfolio_assessments",
    "canonical_portfolio_assessment_payload",
    "portfolio_assessment_fingerprint",
]
