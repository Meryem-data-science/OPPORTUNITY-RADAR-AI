import "server-only";

const DEFAULT_API_BASE_URL = "http://127.0.0.1:8000";
const OPPORTUNITIES_PATH = "/api/opportunities";

export * from "./opportunity-contract";

import {
  OPPORTUNITIES_MAX_LIMIT,
  OPPORTUNITIES_PAGE_SIZE,
  isOpportunitiesResponse,
  type OpportunitiesResponse,
} from "./opportunity-contract";

/**
 * One page of opportunities.
 *
 * `limit` and `offset` are validated here as well as by the backend. The
 * backend answering 422 would be correct but useless to a page: a bad page
 * request is a bug in this app, and it is worth failing where it was made.
 */
export async function getOpportunities(
  limit: number = OPPORTUNITIES_PAGE_SIZE,
  offset: number = 0,
): Promise<OpportunitiesResponse> {
  if (!Number.isInteger(limit) || limit < 1 || limit > OPPORTUNITIES_MAX_LIMIT) {
    throw new Error("Opportunity page limit is out of range");
  }
  if (!Number.isInteger(offset) || offset < 0) {
    throw new Error("Opportunity page offset is out of range");
  }
  const baseUrl = (process.env.OPPORTUNITY_API_BASE_URL ?? DEFAULT_API_BASE_URL).replace(
    /\/$/,
    "",
  );
  const response = await fetch(
    `${baseUrl}${OPPORTUNITIES_PATH}?limit=${limit}&offset=${offset}`,
    {
      cache: "no-store",
      signal: AbortSignal.timeout(5_000),
    },
  );

  if (!response.ok) {
    throw new Error("Opportunity API request failed");
  }

  const payload: unknown = await response.json();
  if (!isOpportunitiesResponse(payload)) {
    throw new Error("Opportunity API response is invalid");
  }

  return payload;
}

/** One page, or null when the listing cannot be read right now. */
export async function loadOpportunities(
  limit: number = OPPORTUNITIES_PAGE_SIZE,
  offset: number = 0,
): Promise<OpportunitiesResponse | null> {
  try {
    return await getOpportunities(limit, offset);
  } catch {
    return null;
  }
}
