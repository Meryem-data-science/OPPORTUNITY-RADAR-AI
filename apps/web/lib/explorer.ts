import "server-only";
import { isOptionalAvailability, type OpportunityAvailability } from "./availability";

/**
 * Server-side read of the Explorer Data & AI surface (Phase 11.1C API).
 *
 * Navigation over persisted Data/AI opportunities, independent of any profile:
 * no profile is ever sent, nothing is ranked, scored, classified or resolved
 * here, and the items keep exactly the order the API returned.
 */
const DEFAULT_API_BASE_URL = "http://127.0.0.1:8000";
const EXPLORER_PATH = "/api/explorer";

export const EXPLORER_FINE_CATEGORIES = [
  "DATA_SCIENCE", "DATA_ANALYTICS", "DATA_ENGINEERING", "MACHINE_LEARNING", "ARTIFICIAL_INTELLIGENCE",
  "GENERATIVE_AI", "NLP", "COMPUTER_VISION", "BUSINESS_INTELLIGENCE", "MLOPS", "OTHER",
] as const;
export type ExplorerFineCategory = (typeof EXPLORER_FINE_CATEGORIES)[number];

export const EXPLORER_OPPORTUNITY_TYPES = [
  "PFA", "PFE", "SUMMER_INTERNSHIP", "PRE_HIRE_INTERNSHIP", "ALTERNANCE", "INTERNSHIP", "FIRST_JOB", "JUNIOR_ROLE",
] as const;
export type ExplorerOpportunityType = (typeof EXPLORER_OPPORTUNITY_TYPES)[number];

export const EXPLORER_OFFER_CATEGORIES = ["STAGES", "PFE", "EMPLOIS", "ALTERNANCE"] as const;
export type ExplorerOfferCategory = (typeof EXPLORER_OFFER_CATEGORIES)[number];

export const EXPLORER_LISTING_TYPES = ["PFE", "INTERNSHIP", "GRADUATE", "APPRENTICESHIP", "JOB", "UNKNOWN"] as const;
export type ExplorerListingType = (typeof EXPLORER_LISTING_TYPES)[number];

export const EXPLORER_FRESHNESS = ["24h", "7d", "30d"] as const;
export type ExplorerFreshness = (typeof EXPLORER_FRESHNESS)[number];

export const EXPLORER_DEFAULT_LIMIT = 24;
export const EXPLORER_MAX_LIMIT = 50;

export type ExplorerResolvedLocation = { country_code: string; city_key: string | null };
export type ExplorerSource = { source_id: string; source_type: string };
export type ExplorerItem = {
  opportunity_id: number;
  canonical_title: string;
  organization: string;
  raw_location: string | null;
  original_url: string;
  last_seen_at: string;
  availability?: OpportunityAvailability;
  /** Persisted Phase 8 value; null is not OTHER. */
  fine_primary_category: ExplorerFineCategory | null;
  /** Persisted Phase 3.5 value; null means unknown. */
  opportunity_type: ExplorerOpportunityType | null;
  /** Persisted qualification value; optional for older API payloads. */
  listing_type?: ExplorerListingType | null;
  resolved_locations: ExplorerResolvedLocation[];
  has_unresolved_location: boolean;
  sources: ExplorerSource[];
};
export type ExplorerCityOption = { country_code: string; city_key: string };
export type ExplorerAvailableFilters = {
  countries: string[];
  cities: ExplorerCityOption[];
  opportunity_types: ExplorerOpportunityType[];
  domains: ExplorerFineCategory[];
  sources: ExplorerSource[];
};
export type ExplorerResponse = {
  items: ExplorerItem[];
  returned: number;
  total: number;
  limit: number;
  offset: number;
  available_filters: ExplorerAvailableFilters;
  verified_only?: boolean;
};

export type ExplorerQuery = {
  country?: string;
  city?: string;
  offer_category?: ExplorerOfferCategory;
  opportunity_type?: ExplorerOpportunityType;
  domain?: ExplorerFineCategory;
  source?: string;
  freshness?: ExplorerFreshness;
  verified_only?: boolean;
  limit?: number;
  offset?: number;
};

/** The supported query keys, in the order they are emitted. */
export const EXPLORER_QUERY_KEYS = [
  "country", "city", "offer_category", "opportunity_type", "domain", "source", "freshness", "verified_only", "limit", "offset",
] as const;

const object = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null && !Array.isArray(value);
const nullableString = (value: unknown) => typeof value === "string" || value === null;
const count = (value: unknown): value is number => Number.isInteger(value) && (value as number) >= 0;
const member = <T extends string>(vocabulary: readonly T[]) => (value: unknown): value is T => (vocabulary as readonly unknown[]).includes(value);
const fineCategory = member(EXPLORER_FINE_CATEGORIES);
const opportunityType = member(EXPLORER_OPPORTUNITY_TYPES);
const offerCategory = member(EXPLORER_OFFER_CATEGORIES);
const listingType = member(EXPLORER_LISTING_TYPES);

function isSource(value: unknown): value is ExplorerSource {
  return object(value) && typeof value.source_id === "string" && typeof value.source_type === "string";
}

function isResolvedLocation(value: unknown): value is ExplorerResolvedLocation {
  return object(value) && typeof value.country_code === "string" && nullableString(value.city_key);
}

function isItem(value: unknown): value is ExplorerItem {
  if (!object(value)) return false;
  return Number.isInteger(value.opportunity_id) && typeof value.canonical_title === "string" &&
    typeof value.organization === "string" && nullableString(value.raw_location) &&
    typeof value.original_url === "string" && typeof value.last_seen_at === "string" &&
    isOptionalAvailability(value.availability) &&
    (value.fine_primary_category === null || fineCategory(value.fine_primary_category)) &&
    (value.opportunity_type === null || opportunityType(value.opportunity_type)) &&
    (value.listing_type === undefined || value.listing_type === null || listingType(value.listing_type)) &&
    Array.isArray(value.resolved_locations) && value.resolved_locations.every(isResolvedLocation) &&
    typeof value.has_unresolved_location === "boolean" &&
    Array.isArray(value.sources) && value.sources.every(isSource);
}

function isAvailableFilters(value: unknown): value is ExplorerAvailableFilters {
  if (!object(value)) return false;
  return Array.isArray(value.countries) && value.countries.every((code) => typeof code === "string") &&
    Array.isArray(value.cities) && value.cities.every((city) => object(city) && typeof city.country_code === "string" && typeof city.city_key === "string") &&
    Array.isArray(value.opportunity_types) && value.opportunity_types.every(opportunityType) &&
    Array.isArray(value.domains) && value.domains.every(fineCategory) &&
    Array.isArray(value.sources) && value.sources.every(isSource);
}

function isExplorerResponse(value: unknown): value is ExplorerResponse {
  if (!object(value) || !Array.isArray(value.items)) return false;
  return value.items.every(isItem) && count(value.returned) && count(value.total) && count(value.limit) &&
    count(value.offset) && value.returned === value.items.length && isAvailableFilters(value.available_filters) &&
    (value.verified_only === undefined || typeof value.verified_only === "boolean");
}

/** Only defined values become parameters; URLSearchParams encodes them. */
export function explorerSearchParams(query: ExplorerQuery): URLSearchParams {
  if (query.offer_category !== undefined && !offerCategory(query.offer_category)) {
    throw new Error("Explorer offer category is invalid");
  }
  if (query.verified_only !== undefined && typeof query.verified_only !== "boolean") {
    throw new Error("Explorer availability filter is invalid");
  }
  const parameters = new URLSearchParams();
  for (const key of EXPLORER_QUERY_KEYS) {
    const value = query[key];
    if (value !== undefined) parameters.set(key, String(value));
  }
  return parameters;
}

export async function getExplorer(query: ExplorerQuery = {}): Promise<ExplorerResponse> {
  const baseUrl = (process.env.OPPORTUNITY_API_BASE_URL ?? DEFAULT_API_BASE_URL).replace(/\/$/, "");
  const search = explorerSearchParams(query).toString();
  const url = `${baseUrl}${EXPLORER_PATH}${search ? `?${search}` : ""}`;
  const response = await fetch(url, { cache: "no-store", signal: AbortSignal.timeout(5_000) });
  if (!response.ok) throw new Error("Explorer API request failed");
  const payload: unknown = await response.json();
  if (!isExplorerResponse(payload)) throw new Error("Explorer API response is invalid");
  // An older API may ignore verified_only. Never present its unverified page
  // as confirmed, or filter one page locally and misrepresent the total.
  if (query.verified_only === true &&
    (payload.verified_only !== true ||
      payload.items.some((item) => item.availability?.status !== "OPEN_CONFIRMED"))) {
    throw new Error("Explorer availability filter was not confirmed by the API");
  }
  return payload;
}

export async function loadExplorer(query: ExplorerQuery = {}): Promise<ExplorerResponse | null> {
  try { return await getExplorer(query); } catch { return null; }
}
