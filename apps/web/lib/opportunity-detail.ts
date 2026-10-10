import "server-only";

const DEFAULT_API_BASE_URL = "http://127.0.0.1:8000";
const LISTING_TYPES = ["PFE", "INTERNSHIP", "GRADUATE", "APPRENTICESHIP", "JOB", "UNKNOWN"] as const;

export type OpportunityListingType = (typeof LISTING_TYPES)[number];
export type OpportunityDetail = {
  id: number;
  canonical_title: string;
  organization: string;
  location: string | null;
  original_url: string;
  discovered_at: string;
  last_seen_at: string;
  description: string | null;
  published_at: string | null;
  employment_type: string | null;
  listing_type: OpportunityListingType | null;
};

export type OpportunityDetailResult =
  | { kind: "ready"; opportunity: OpportunityDetail }
  | { kind: "not-found" }
  | { kind: "unavailable" };

const object = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const nullableText = (value: unknown) => typeof value === "string" || value === null;

function validDetail(value: unknown): value is OpportunityDetail {
  if (!object(value)) return false;
  return Number.isSafeInteger(value.id) && (value.id as number) > 0 &&
    typeof value.canonical_title === "string" && typeof value.organization === "string" &&
    nullableText(value.location) && typeof value.original_url === "string" &&
    typeof value.discovered_at === "string" && typeof value.last_seen_at === "string" &&
    nullableText(value.description) && nullableText(value.published_at) &&
    nullableText(value.employment_type) &&
    (value.listing_type === null || (LISTING_TYPES as readonly unknown[]).includes(value.listing_type));
}

/** Read the saved description. No profile, ranking or external ATS request is needed. */
export async function loadOpportunityDetail(id: number): Promise<OpportunityDetailResult> {
  if (!Number.isSafeInteger(id) || id < 1) return { kind: "not-found" };
  const baseUrl = (process.env.OPPORTUNITY_API_BASE_URL ?? DEFAULT_API_BASE_URL).replace(/\/$/, "");
  try {
    const response = await fetch(`${baseUrl}/api/opportunities/${id}`, {
      cache: "no-store",
      signal: AbortSignal.timeout(5_000),
    });
    if (response.status === 404) return { kind: "not-found" };
    if (!response.ok) return { kind: "unavailable" };
    const payload: unknown = await response.json();
    if (!validDetail(payload) || payload.id !== id) return { kind: "unavailable" };
    return { kind: "ready", opportunity: payload };
  } catch {
    return { kind: "unavailable" };
  }
}
