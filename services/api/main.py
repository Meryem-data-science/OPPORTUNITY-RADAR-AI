"""FastAPI application exposing persisted opportunity data read-only."""

from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, status

from services.api.opportunities import (
    OpportunityListResponse,
    OpportunityReadError,
    PUBLIC_DATABASE_ERROR,
    read_opportunities,
)
from services.api.source_health import (
    PUBLIC_SOURCE_HEALTH_ERROR,
    SourceHealthListResponse,
    SourceHealthReadError,
    read_source_health,
)
from services.api.matching import (
    MatchingApiReadError,
    MatchingResponse,
    PUBLIC_MATCHING_ERROR,
    read_matching_surface,
)
from services.api.priority import (
    PUBLIC_PRIORITY_ERROR,
    PriorityApiReadError,
    PriorityResponse,
    read_priority_surface,
)


app = FastAPI(title="Opportunity Radar API", version="1.5.0")


@app.get("/api/opportunities", response_model=OpportunityListResponse)
def list_opportunities(
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> OpportunityListResponse:
    """Return the most recently observed visible, active opportunities."""
    try:
        return read_opportunities(limit)
    except OpportunityReadError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=PUBLIC_DATABASE_ERROR,
        ) from None


@app.get("/api/source-health", response_model=SourceHealthListResponse)
def list_source_health() -> SourceHealthListResponse:
    """Return one deterministic health entry per known source, read-only."""
    try:
        return read_source_health()
    except SourceHealthReadError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=PUBLIC_SOURCE_HEALTH_ERROR,
        ) from None


@app.get("/api/matching", response_model=MatchingResponse)
def get_matching() -> MatchingResponse:
    """Return the audited persisted matching snapshot for the server profile."""
    try:
        return read_matching_surface()
    except MatchingApiReadError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=PUBLIC_MATCHING_ERROR,
        ) from None


@app.get("/api/priority", response_model=PriorityResponse)
def get_priority() -> PriorityResponse:
    """Return the audited persisted Priority snapshot for the server profile."""
    try:
        return read_priority_surface()
    except PriorityApiReadError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=PUBLIC_PRIORITY_ERROR,
        ) from None
