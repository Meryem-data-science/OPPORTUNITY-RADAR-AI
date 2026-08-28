import "server-only";

const DEFAULT_API_BASE_URL = "http://127.0.0.1:8000";
const SOURCE_HEALTH_PATH = "/api/source-health";

/**
 * One source exactly as the backend read model describes it.
 *
 * Every nullable field means "the backend does not know", never zero, and the
 * anomaly fields are the backend's decision. Nothing here is re-derived.
 */
export type SourceHealthEntry = {
  source_id: string;
  enabled: boolean;
  last_run_at: string | null;
  status: string | null;
  items_found: number | null;
  new_items: number | null;
  relevant_items: number | null;
  error_type: string | null;
  error_message: string | null;
  zero_result_streak: number;
  anomaly_code: string | null;
  anomaly_message: string | null;
};

export type SourceHealthResponse = {
  items: SourceHealthEntry[];
  returned: number;
};

function isNullableString(value: unknown): boolean {
  return typeof value === "string" || value === null;
}

function isNullableNumber(value: unknown): boolean {
  return typeof value === "number" || value === null;
}

function isSourceHealthEntry(value: unknown): value is SourceHealthEntry {
  if (typeof value !== "object" || value === null) return false;

  const entry = value as Record<string, unknown>;
  return (
    typeof entry.source_id === "string" &&
    typeof entry.enabled === "boolean" &&
    isNullableString(entry.last_run_at) &&
    isNullableString(entry.status) &&
    isNullableNumber(entry.items_found) &&
    isNullableNumber(entry.new_items) &&
    isNullableNumber(entry.relevant_items) &&
    isNullableString(entry.error_type) &&
    isNullableString(entry.error_message) &&
    typeof entry.zero_result_streak === "number" &&
    isNullableString(entry.anomaly_code) &&
    isNullableString(entry.anomaly_message)
  );
}

function isSourceHealthResponse(value: unknown): value is SourceHealthResponse {
  if (typeof value !== "object" || value === null) return false;

  const response = value as Record<string, unknown>;
  return (
    Array.isArray(response.items) &&
    response.items.every(isSourceHealthEntry) &&
    typeof response.returned === "number"
  );
}

export async function getSourceHealth(): Promise<SourceHealthResponse> {
  const baseUrl = (process.env.OPPORTUNITY_API_BASE_URL ?? DEFAULT_API_BASE_URL).replace(
    /\/$/,
    "",
  );
  const response = await fetch(`${baseUrl}${SOURCE_HEALTH_PATH}`, {
    cache: "no-store",
    signal: AbortSignal.timeout(5_000),
  });

  if (!response.ok) {
    throw new Error("Source health API request failed");
  }

  const payload: unknown = await response.json();
  if (!isSourceHealthResponse(payload)) {
    throw new Error("Source health API response is invalid");
  }

  return payload;
}

export async function loadSourceHealth(): Promise<SourceHealthResponse | null> {
  try {
    return await getSourceHealth();
  } catch {
    return null;
  }
}
