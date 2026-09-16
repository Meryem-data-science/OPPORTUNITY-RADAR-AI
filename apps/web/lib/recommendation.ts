import "server-only";

/**
 * Server-side read of the persisted Phase 9 Recommendation (Phase 11.1A API).
 *
 * The profile is never passed: FastAPI resolves the configured profile itself.
 * The ranking is the backend's; nothing here reorders, rescores or reclassifies.
 */
const DEFAULT_API_BASE_URL = "http://127.0.0.1:8000";
const RECOMMENDATION_PATH = "/api/recommendation";

export const RECOMMENDATION_DISPOSITIONS = ["RECOMMENDED", "UNCERTAIN", "OUTSIDE_PREFERENCES", "KNOWN_BLOCKER"] as const;
export type RecommendationDisposition = (typeof RECOMMENDATION_DISPOSITIONS)[number];

export const FINE_CATEGORIES = [
  "DATA_SCIENCE", "DATA_ANALYTICS", "DATA_ENGINEERING", "MACHINE_LEARNING", "ARTIFICIAL_INTELLIGENCE",
  "GENERATIVE_AI", "NLP", "COMPUTER_VISION", "BUSINESS_INTELLIGENCE", "MLOPS", "OTHER",
] as const;
export type FineCategory = (typeof FINE_CATEGORIES)[number];

export type FineEvidence = { category: FineCategory; field: "TITLE" | "DESCRIPTION"; kind: string; signal: string };
export type RecommendationOpportunity = {
  id: number; canonical_title: string; organization: string; location: string | null;
  last_seen_at: string; original_url: string;
  fine_primary_category: FineCategory | null;
  fine_secondary_categories: FineCategory[] | null;
  fine_category_evidence: FineEvidence[] | null;
  fine_reasons: string[] | null;
  fine_classifier_version: string | null;
};
export type RecommendationSnapshot = {
  disposition: RecommendationDisposition;
  /** null means not available; it is never a zero. */
  recommendation_score: number | null;
  evidence_coverage: number;
  assessment_fingerprint: string;
  strengths: string[];
  confirmed_gaps: string[];
  unknowns: string[];
  explanation: Record<string, unknown>;
};
export type RecommendationItem = {
  opportunity_id: number; rank_position: number;
  opportunity: RecommendationOpportunity; recommendation: RecommendationSnapshot;
};
export type RecommendationRun = {
  run_id: number; created_at: string; assessment_count: number;
  persistence_version: string; input_assembly_version: string;
  recommendation_engine_version: string; recommendation_rules_version: string;
  source_matching_run_id: number; source_matching_run_fingerprint: string;
  batch_fingerprint: string; run_fingerprint: string;
  items: RecommendationItem[];
};
export type RecommendationStatus = "NOT_SYNCED" | "INCOMPLETE" | "READY";
export type RecommendationReadinessIssue = { code: string; opportunity_id: number | null };
export type RecommendationResponse = {
  profile_id: number; status: RecommendationStatus;
  persistence_version: string | null; input_assembly_version: string | null;
  history_count: number; readiness_issues: RecommendationReadinessIssue[];
  current_run: RecommendationRun | null;
  integrity: { ok: boolean; audit_version: string; audit_fingerprint: string };
};

const object = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null && !Array.isArray(value);
const nullableString = (value: unknown) => typeof value === "string" || value === null;
const finiteNumber = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);
const stringList = (value: unknown): value is string[] => Array.isArray(value) && value.every((code) => typeof code === "string");
const fineCategory = (value: unknown): value is FineCategory => (FINE_CATEGORIES as readonly unknown[]).includes(value);

function isEvidence(value: unknown): value is FineEvidence {
  return object(value) && fineCategory(value.category) && (value.field === "TITLE" || value.field === "DESCRIPTION") &&
    typeof value.kind === "string" && typeof value.signal === "string";
}

function isOpportunity(value: unknown): value is RecommendationOpportunity {
  if (!object(value)) return false;
  return Number.isInteger(value.id) && typeof value.canonical_title === "string" &&
    typeof value.organization === "string" && nullableString(value.location) &&
    typeof value.last_seen_at === "string" && typeof value.original_url === "string" &&
    (value.fine_primary_category === null || fineCategory(value.fine_primary_category)) &&
    (value.fine_secondary_categories === null || (Array.isArray(value.fine_secondary_categories) && value.fine_secondary_categories.every(fineCategory))) &&
    (value.fine_category_evidence === null || (Array.isArray(value.fine_category_evidence) && value.fine_category_evidence.every(isEvidence))) &&
    (value.fine_reasons === null || stringList(value.fine_reasons)) &&
    nullableString(value.fine_classifier_version);
}

function isSnapshot(value: unknown): value is RecommendationSnapshot {
  if (!object(value)) return false;
  return (RECOMMENDATION_DISPOSITIONS as readonly unknown[]).includes(value.disposition) &&
    (value.recommendation_score === null || finiteNumber(value.recommendation_score)) &&
    finiteNumber(value.evidence_coverage) && typeof value.assessment_fingerprint === "string" &&
    stringList(value.strengths) && stringList(value.confirmed_gaps) && stringList(value.unknowns) &&
    // The explanation is the engine's own payload: only its shape is checked.
    object(value.explanation);
}

function isItem(value: unknown): value is RecommendationItem {
  if (!object(value) || !isOpportunity(value.opportunity) || !isSnapshot(value.recommendation)) return false;
  return Number.isInteger(value.opportunity_id) && Number.isInteger(value.rank_position) &&
    value.opportunity.id === value.opportunity_id;
}

const runStrings = [
  "created_at", "persistence_version", "input_assembly_version", "recommendation_engine_version",
  "recommendation_rules_version", "source_matching_run_fingerprint", "batch_fingerprint", "run_fingerprint",
] as const;

function isRun(value: unknown): value is RecommendationRun {
  if (!object(value) || !Array.isArray(value.items)) return false;
  return Number.isInteger(value.run_id) && Number.isInteger(value.assessment_count) &&
    Number.isInteger(value.source_matching_run_id) && runStrings.every((key) => typeof value[key] === "string") &&
    value.items.every(isItem) && value.items.length === value.assessment_count;
}

function isReadinessIssue(value: unknown): value is RecommendationReadinessIssue {
  return object(value) && typeof value.code === "string" && (value.opportunity_id === null || Number.isInteger(value.opportunity_id));
}

function isRecommendationResponse(value: unknown): value is RecommendationResponse {
  if (!object(value) || !object(value.integrity)) return false;
  if (!["NOT_SYNCED", "INCOMPLETE", "READY"].includes(value.status as string) || !Number.isInteger(value.profile_id) ||
      !nullableString(value.persistence_version) || !nullableString(value.input_assembly_version) ||
      !Number.isInteger(value.history_count) || !Array.isArray(value.readiness_issues) ||
      !value.readiness_issues.every(isReadinessIssue) || typeof value.integrity.ok !== "boolean" ||
      typeof value.integrity.audit_version !== "string" || typeof value.integrity.audit_fingerprint !== "string") return false;
  // Only READY carries a run: an older run is never presented as current.
  return value.status === "READY" ? isRun(value.current_run) : value.current_run === null;
}

export async function getRecommendation(): Promise<RecommendationResponse> {
  const baseUrl = (process.env.OPPORTUNITY_API_BASE_URL ?? DEFAULT_API_BASE_URL).replace(/\/$/, "");
  const response = await fetch(`${baseUrl}${RECOMMENDATION_PATH}`, { cache: "no-store", signal: AbortSignal.timeout(5_000) });
  if (!response.ok) throw new Error("Recommendation API request failed");
  const payload: unknown = await response.json();
  if (!isRecommendationResponse(payload)) throw new Error("Recommendation API response is invalid");
  return payload;
}

export async function loadRecommendation(): Promise<RecommendationResponse | null> {
  try { return await getRecommendation(); } catch { return null; }
}
