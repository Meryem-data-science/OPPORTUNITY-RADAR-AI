import "server-only";

const DEFAULT_API_BASE_URL = "http://127.0.0.1:8000";
const OPPORTUNITIES_PATH = "/api/opportunities?limit=20";

export type Opportunity = {
  id: number;
  canonical_title: string;
  organization: string;
  location: string | null;
  original_url: string;
  last_seen_at: string;
};

export type OpportunitiesResponse = {
  items: Opportunity[];
  returned: number;
  total: number;
};

function isOpportunity(value: unknown): value is Opportunity {
  if (typeof value !== "object" || value === null) return false;

  const opportunity = value as Record<string, unknown>;
  return (
    typeof opportunity.id === "number" &&
    typeof opportunity.canonical_title === "string" &&
    typeof opportunity.organization === "string" &&
    (typeof opportunity.location === "string" || opportunity.location === null) &&
    typeof opportunity.original_url === "string" &&
    typeof opportunity.last_seen_at === "string"
  );
}

function isOpportunitiesResponse(value: unknown): value is OpportunitiesResponse {
  if (typeof value !== "object" || value === null) return false;

  const response = value as Record<string, unknown>;
  return (
    Array.isArray(response.items) &&
    response.items.every(isOpportunity) &&
    typeof response.returned === "number" &&
    typeof response.total === "number"
  );
}

export async function getOpportunities(): Promise<OpportunitiesResponse> {
  const baseUrl = (process.env.OPPORTUNITY_API_BASE_URL ?? DEFAULT_API_BASE_URL).replace(
    /\/$/,
    "",
  );
  const response = await fetch(`${baseUrl}${OPPORTUNITIES_PATH}`, {
    cache: "no-store",
    signal: AbortSignal.timeout(5_000),
  });

  if (!response.ok) {
    throw new Error("Opportunity API request failed");
  }

  const payload: unknown = await response.json();
  if (!isOpportunitiesResponse(payload)) {
    throw new Error("Opportunity API response is invalid");
  }

  return payload;
}

export async function loadOpportunities(): Promise<OpportunitiesResponse | null> {
  try {
    return await getOpportunities();
  } catch {
    return null;
  }
}
