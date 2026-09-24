"""FastAPI application: the read surfaces of the radar, and the writes a person makes.

Everything the radar itself produces — opportunities, matching, priority, the
Portfolio, the Recommendation, the Explorer, source health — is exposed read-only. The two write surfaces exist
because a person acted: opting a browser into notifications, and tracking a
candidature. Both resolve the profile on the server; neither accepts one.
"""

from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Request, Response, status

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
from services.api.recommendation import (
    PUBLIC_RECOMMENDATION_ERROR,
    RecommendationApiReadError,
    RecommendationResponse,
    read_recommendation_surface,
)
from services.api.explorer import (
    DEFAULT_LIMIT as EXPLORER_DEFAULT_LIMIT,
    MAX_LIMIT as EXPLORER_MAX_LIMIT,
    PUBLIC_EXPLORER_ERROR,
    ExplorerApiReadError,
    ExplorerFreshness,
    ExplorerQuery,
    ExplorerResponse,
    read_explorer_surface,
)
from services.collector.qualification.fine_taxonomy import FineCategory
from services.digital_twin.preferences.models import OpportunityType
from services.api.applications import (
    PUBLIC_APPLICATION_ERROR,
    PUBLIC_APPLICATION_REQUEST_ERROR,
    ApplicationApiConflictError,
    ApplicationApiError,
    ApplicationApiNotFoundError,
    ApplicationApiRequestError,
    ApplicationDetailResponse,
    ApplicationListResponse,
    ApplicationWriteResponse,
    change_status_surface,
    create_application_surface,
    list_applications_surface,
    read_application_surface,
    update_tracking_surface,
)


app = FastAPI(title="Opportunity Radar API", version="1.6.0")


@app.get("/api/opportunities", response_model=OpportunityListResponse)
def list_opportunities(
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> OpportunityListResponse:
    """Return the most recently observed visible, active opportunities.

    `limit` keeps its ceiling of 100 per request: paging is how a caller reads
    more than that, not a bigger single answer. `offset` says where the page
    starts, and both are validated here rather than inside the reader, so an
    absurd value is a 422 from FastAPI and never reaches SQL.
    """
    try:
        return read_opportunities(limit, offset)
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


@app.get("/api/recommendation", response_model=RecommendationResponse)
def get_recommendation() -> RecommendationResponse:
    """Return the audited persisted Recommendation for the server profile."""
    try:
        return read_recommendation_surface()
    except RecommendationApiReadError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=PUBLIC_RECOMMENDATION_ERROR,
        ) from None


@app.get("/api/explorer", response_model=ExplorerResponse)
def get_explorer(
    country: Annotated[str | None, Query(pattern=r"^[A-Z]{2}$")] = None,
    city: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    opportunity_type: OpportunityType | None = None,
    domain: FineCategory | None = None,
    source: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    freshness: ExplorerFreshness | None = None,
    limit: Annotated[int, Query(ge=1, le=EXPLORER_MAX_LIMIT)] = EXPLORER_DEFAULT_LIMIT,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ExplorerResponse:
    """Explore the persisted Data/AI corpus; no profile, no score, no ranking."""
    try:
        return read_explorer_surface(
            ExplorerQuery(
                country=country,
                city=city,
                opportunity_type=opportunity_type,
                domain=domain,
                source=source,
                freshness=freshness,
                limit=limit,
                offset=offset,
            )
        )
    except ExplorerApiReadError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=PUBLIC_EXPLORER_ERROR,
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


async def _application_body(request: Request) -> object:
    """Read a JSON body without letting a parser echo it back to the caller."""
    try:
        return await request.json()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=PUBLIC_APPLICATION_REQUEST_ERROR,
        ) from None


def _application_failure(error: Exception) -> HTTPException:
    """Map one domain refusal onto the answer the caller is owed.

    A malformed request is 400, something this profile does not have is 404, a
    request that contradicts the candidature is 409, and anything else — an
    unusable database, a missing migration, a broken configuration — is 503
    with a fixed sentence. No internal message ever escapes through here.
    """
    if isinstance(error, ApplicationApiRequestError):
        return HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)
        )
    if isinstance(error, ApplicationApiNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error))
    if isinstance(error, ApplicationApiConflictError):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=PUBLIC_APPLICATION_ERROR,
    )


_APPLICATION_ERRORS = (
    ApplicationApiRequestError,
    ApplicationApiNotFoundError,
    ApplicationApiConflictError,
    ApplicationApiError,
)


@app.get("/api/applications", response_model=ApplicationListResponse)
def list_applications() -> ApplicationListResponse:
    """Return every tracked candidature of the server-resolved profile."""
    try:
        return list_applications_surface()
    except _APPLICATION_ERRORS as error:
        raise _application_failure(error) from None


@app.get(
    "/api/applications/{application_id}", response_model=ApplicationDetailResponse
)
def get_application(application_id: int) -> ApplicationDetailResponse:
    """Return one tracked candidature with its append-only timeline."""
    try:
        return read_application_surface(application_id)
    except _APPLICATION_ERRORS as error:
        raise _application_failure(error) from None


@app.post("/api/applications", response_model=ApplicationWriteResponse)
async def create_application(
    request: Request, response: Response
) -> ApplicationWriteResponse:
    """Record the intention that makes a real opportunity a candidature.

    201 when this call created the candidature, 200 when it already existed —
    a repeated action is answered with the unchanged candidature rather than a
    duplicate, and the body says which of the two happened.
    """
    body = await _application_body(request)
    try:
        result = create_application_surface(body)
    except _APPLICATION_ERRORS as error:
        raise _application_failure(error) from None
    response.status_code = (
        status.HTTP_201_CREATED if result.created else status.HTTP_200_OK
    )
    return result


@app.patch(
    "/api/applications/{application_id}/status",
    response_model=ApplicationWriteResponse,
)
async def patch_application_status(
    application_id: int, request: Request
) -> ApplicationWriteResponse:
    """Move one candidature by hand, within the rules Phase 6.1 enforces."""
    body = await _application_body(request)
    try:
        return change_status_surface(application_id, body)
    except _APPLICATION_ERRORS as error:
        raise _application_failure(error) from None


@app.patch("/api/applications/{application_id}", response_model=ApplicationWriteResponse)
async def patch_application_tracking(
    application_id: int, request: Request
) -> ApplicationWriteResponse:
    """Write the manual tracking fields of one candidature, and only those."""
    body = await _application_body(request)
    try:
        return update_tracking_surface(application_id, body)
    except _APPLICATION_ERRORS as error:
        raise _application_failure(error) from None
