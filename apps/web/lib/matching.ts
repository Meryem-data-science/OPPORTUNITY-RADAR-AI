import "server-only";

const DEFAULT_API_BASE_URL = "http://127.0.0.1:8000";
const MATCHING_PATH = "/api/matching";

export type MatchingLane = "PRIMARY" | "UNCERTAIN" | "OUTSIDE_PREFERENCES";
export type MatchingOpportunity = { id: number; canonical_title: string; organization: string; location: string | null; last_seen_at: string; original_url: string };
export type MatchingSnapshot = { lane: MatchingLane; match_quality: number | null; evidence_coverage: number; assessment_fingerprint: string; explanation: Record<string, unknown> };
export type MatchingItem = { opportunity_id: number; opportunity: MatchingOpportunity; matching: MatchingSnapshot };
export type MatchingRun = {
  run_id: number; created_at: string; assessment_count: number;
  persistence_version: string; selection_version: string; matching_engine_version: string;
  matching_rules_version: string; semantic_percentile_version: string;
  run_fingerprint: string; batch_fingerprint: string;
  lane_counts: Record<MatchingLane, number>; items: MatchingItem[];
};
export type MatchingResponse = {
  profile_id: number; status: "NOT_SYNCED" | "EMPTY" | "READY";
  persistence_version: string | null; selection_version: string | null;
  history_count: number; current_run: MatchingRun | null;
  integrity: { ok: boolean; audit_version: string; audit_fingerprint: string };
};

const lanes: MatchingLane[] = ["PRIMARY", "UNCERTAIN", "OUTSIDE_PREFERENCES"];
const object = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null && !Array.isArray(value);
const nullableString = (value: unknown) => typeof value === "string" || value === null;
const finiteNumber = (value: unknown) => typeof value === "number" && Number.isFinite(value);

function isItem(value: unknown): value is MatchingItem {
  if (!object(value) || !object(value.opportunity) || !object(value.matching)) return false;
  const opportunity = value.opportunity;
  const matching = value.matching;
  return Number.isInteger(value.opportunity_id) && Number.isInteger(opportunity.id) &&
    typeof opportunity.canonical_title === "string" && typeof opportunity.organization === "string" &&
    nullableString(opportunity.location) && typeof opportunity.last_seen_at === "string" &&
    typeof opportunity.original_url === "string" && lanes.includes(matching.lane as MatchingLane) &&
    (matching.match_quality === null || finiteNumber(matching.match_quality)) &&
    finiteNumber(matching.evidence_coverage) && typeof matching.assessment_fingerprint === "string" &&
    object(matching.explanation);
}

function isRun(value: unknown): value is MatchingRun {
  if (!object(value) || !object(value.lane_counts) || !Array.isArray(value.items)) return false;
  const laneCounts = value.lane_counts;
  return Number.isInteger(value.run_id) && typeof value.created_at === "string" &&
    Number.isInteger(value.assessment_count) &&
    ["persistence_version", "selection_version", "matching_engine_version", "matching_rules_version", "semantic_percentile_version", "run_fingerprint", "batch_fingerprint"].every((key) => typeof value[key] === "string") &&
    Object.keys(laneCounts).length === 3 && lanes.every((lane) => Number.isInteger(laneCounts[lane])) &&
    value.items.every(isItem) && value.items.length === value.assessment_count;
}

function isMatchingResponse(value: unknown): value is MatchingResponse {
  if (!object(value) || !object(value.integrity)) return false;
  const statuses = ["NOT_SYNCED", "EMPTY", "READY"];
  if (!statuses.includes(value.status as string) || !Number.isInteger(value.profile_id) ||
      !nullableString(value.persistence_version) || !nullableString(value.selection_version) ||
      !Number.isInteger(value.history_count) || typeof value.integrity.ok !== "boolean" ||
      typeof value.integrity.audit_version !== "string" || typeof value.integrity.audit_fingerprint !== "string") return false;
  return value.status === "READY" ? isRun(value.current_run) : value.current_run === null;
}

export async function getMatching(): Promise<MatchingResponse> {
  const baseUrl = (process.env.OPPORTUNITY_API_BASE_URL ?? DEFAULT_API_BASE_URL).replace(/\/$/, "");
  const response = await fetch(`${baseUrl}${MATCHING_PATH}`, { cache: "no-store", signal: AbortSignal.timeout(5_000) });
  if (!response.ok) throw new Error("Matching API request failed");
  const payload: unknown = await response.json();
  if (!isMatchingResponse(payload)) throw new Error("Matching API response is invalid");
  return payload;
}

export async function loadMatching(): Promise<MatchingResponse | null> {
  try { return await getMatching(); } catch { return null; }
}
