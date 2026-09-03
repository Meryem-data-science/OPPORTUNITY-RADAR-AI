"""FastAPI application exposing persisted opportunity data read-only."""

from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Request, status

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
from services.api.push import (
    PUBLIC_PUSH_ERROR,
    PUBLIC_PUSH_REQUEST_ERROR,
    PushApiError,
    PushConfigResponse,
    PushConflictError,
    PushRequestError,
    PushSubscriptionStateResponse,
    read_push_config,
    register_subscription,
    revoke_subscription,
)
from services.api.portfolio import (
    PUBLIC_PORTFOLIO_ERROR,
    PortfolioApiReadError,
    PortfolioResponse,
    read_portfolio_surface,
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


@app.get("/api/portfolio", response_model=PortfolioResponse)
def get_portfolio() -> PortfolioResponse:
    """Return the audited persisted Portfolio snapshot for the server profile."""
    try:
        return read_portfolio_surface()
    except PortfolioApiReadError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=PUBLIC_PORTFOLIO_ERROR,
        ) from None


@app.get("/api/push/config", response_model=PushConfigResponse)
def get_push_config() -> PushConfigResponse:
    """Report whether Web Push is configured and expose only the public key."""
    return read_push_config()


async def _push_body(request: Request) -> object:
    """Read a JSON body without letting a parser echo it back to the caller."""
    try:
        return await request.json()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=PUBLIC_PUSH_REQUEST_ERROR,
        ) from None


def _push_failure(error: Exception) -> HTTPException:
    if isinstance(error, PushRequestError):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(error))
    if isinstance(error, PushConflictError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=PUBLIC_PUSH_ERROR
    )


@app.post(
    "/api/push/subscriptions",
    response_model=PushSubscriptionStateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_push_subscription(
    request: Request,
) -> PushSubscriptionStateResponse:
    """Persist one browser push subscription for the server-resolved profile."""
    body = await _push_body(request)
    try:
        return register_subscription(body)
    except (PushRequestError, PushConflictError, PushApiError) as error:
        raise _push_failure(error) from None


@app.delete("/api/push/subscriptions", response_model=PushSubscriptionStateResponse)
async def delete_push_subscription(
    request: Request,
) -> PushSubscriptionStateResponse:
    """Revoke one browser push subscription for the server-resolved profile."""
    body = await _push_body(request)
    try:
        return revoke_subscription(body)
    except (PushRequestError, PushConflictError, PushApiError) as error:
        raise _push_failure(error) from None
