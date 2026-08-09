"""FastAPI application exposing persisted opportunity data read-only."""

from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, status

from services.api.opportunities import (
    OpportunityListResponse,
    OpportunityReadError,
    PUBLIC_DATABASE_ERROR,
    read_opportunities,
)


app = FastAPI(title="Opportunity Radar API", version="1.4.0")


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
