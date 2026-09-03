import "server-only";

/**
 * Server-side proxy to the Python push surface.
 *
 * The browser talks only to this app's own origin: the backend base URL stays
 * a server value and is never published through a NEXT_PUBLIC variable.
 */
const API_BASE = "http://127.0.0.1:8000";
const REQUEST_TIMEOUT_MS = 5_000;

export type PushConfig = { configured: boolean; vapid_public_key: string | null };
export type PushSubscriptionState = { status: "ACTIVE" | "REVOKED" };
export type PushForwardResult =
  | { ok: true; state: PushSubscriptionState }
  | { ok: false; status: number };

const object = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

function baseUrl(): string {
  return (process.env.OPPORTUNITY_API_BASE_URL ?? API_BASE).replace(/\/$/, "");
}

function validConfig(value: unknown): value is PushConfig {
  if (!object(value) || typeof value.configured !== "boolean") return false;
  const key = value.vapid_public_key;
  if (value.configured) return typeof key === "string" && key.length > 0;
  return key === null;
}

function validState(value: unknown): value is PushSubscriptionState {
  return object(value) && (value.status === "ACTIVE" || value.status === "REVOKED");
}

/** Report the configuration the browser is allowed to see, failing closed. */
export async function getPushConfig(): Promise<PushConfig> {
  try {
    const response = await fetch(`${baseUrl()}/api/push/config`, {
      cache: "no-store",
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    });
    if (!response.ok) return { configured: false, vapid_public_key: null };
    const payload: unknown = await response.json();
    return validConfig(payload) ? payload : { configured: false, vapid_public_key: null };
  } catch {
    return { configured: false, vapid_public_key: null };
  }
}

async function forward(
  method: "POST" | "DELETE",
  body: unknown,
): Promise<PushForwardResult> {
  let response: Response;
  try {
    response = await fetch(`${baseUrl()}/api/push/subscriptions`, {
      method,
      cache: "no-store",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    });
  } catch {
    return { ok: false, status: 503 };
  }
  if (!response.ok) {
    // The backend's own status is kept so a rejected body stays a 4xx, but its
    // message is not relayed: the client only needs to know it failed.
    return { ok: false, status: response.status };
  }
  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    return { ok: false, status: 502 };
  }
  return validState(payload) ? { ok: true, state: payload } : { ok: false, status: 502 };
}

/** Register one browser subscription; the backend resolves the profile. */
export function forwardSubscribe(body: unknown): Promise<PushForwardResult> {
  return forward("POST", body);
}

/** Revoke one browser subscription by endpoint. */
export function forwardUnsubscribe(body: unknown): Promise<PushForwardResult> {
  return forward("DELETE", body);
}
