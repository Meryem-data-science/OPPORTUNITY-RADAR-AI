import "server-only";

/**
 * Server-side access to the Python application-tracking surface.
 *
 * Every read and every write goes through FastAPI. This module runs on the
 * server, the backend base URL never becomes a NEXT_PUBLIC_ variable, and
 * nothing in the browser — or in this file — ever opens the SQLite database:
 * FastAPI is the only thing that writes a candidature, because it is the only
 * thing that knows the rules one obeys.
 *
 * The profile is not passed. The backend resolves the single configured
 * profile itself, so a page cannot ask for somebody else's candidatures.
 */
const API_BASE = "http://127.0.0.1:8000";
const REQUEST_TIMEOUT_MS = 5_000;

export * from "./application-contract";

import {
  APPLICATION_STATUSES,
  type Application,
  type ApplicationDetail,
  type ApplicationEvent,
  type ApplicationOpportunity,
  type ApplicationWriteResult,
  type ApplicationsResponse,
} from "./application-contract";

export type ApplicationForwardResult =
  | { ok: true; status: number; result: ApplicationWriteResult }
  | { ok: false; status: number };

const object = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const identifier = (value: unknown): value is number =>
  typeof value === "number" && Number.isInteger(value) && value > 0;

const text = (value: unknown): value is string => typeof value === "string";

const optionalText = (value: unknown): value is string | null =>
  value === null || typeof value === "string";

function baseUrl(): string {
  return (process.env.OPPORTUNITY_API_BASE_URL ?? API_BASE).replace(/\/$/, "");
}

function validOpportunity(value: unknown): value is ApplicationOpportunity {
  if (!object(value)) return false;
  return (
    identifier(value.id) &&
    text(value.canonical_title) &&
    text(value.organization) &&
    optionalText(value.location) &&
    // The link back to the real offer is the one field this page exists to
    // keep working, so a candidature without one is not rendered at all.
    text(value.original_url) &&
    value.original_url.length > 0
  );
}

function validApplication(value: unknown): value is Application {
  if (!object(value)) return false;
  return (
    identifier(value.id) &&
    identifier(value.opportunity_id) &&
    text(value.status) &&
    (APPLICATION_STATUSES as readonly string[]).includes(value.status) &&
    optionalText(value.submitted_at) &&
    text(value.last_status_change) &&
    optionalText(value.next_action) &&
    optionalText(value.followup_date) &&
    optionalText(value.notes) &&
    text(value.created_at) &&
    text(value.updated_at) &&
    validOpportunity(value.opportunity) &&
    (value.opportunity as ApplicationOpportunity).id === value.opportunity_id
  );
}

function validEvent(value: unknown): value is ApplicationEvent {
  if (!object(value)) return false;
  return (
    identifier(value.id) &&
    ["APPLICATION_CREATED", "STATUS_CHANGED", "TRACKING_UPDATED"].includes(
      value.event_type as string,
    ) &&
    optionalText(value.from_status) &&
    optionalText(value.to_status) &&
    (value.actor_type === "USER" || value.actor_type === "SYSTEM") &&
    text(value.occurred_at)
  );
}

function validDetail(value: unknown): value is ApplicationDetail {
  if (!validApplication(value) || !object(value)) return false;
  const events = (value as Record<string, unknown>).events;
  return Array.isArray(events) && events.every(validEvent);
}

function validListing(value: unknown): value is ApplicationsResponse {
  if (!object(value)) return false;
  return (
    identifier(value.profile_id) &&
    Array.isArray(value.items) &&
    value.items.every(validApplication) &&
    typeof value.total === "number" &&
    value.total === value.items.length
  );
}

function validWrite(value: unknown): value is ApplicationWriteResult {
  if (!object(value)) return false;
  return (
    typeof value.created === "boolean" &&
    typeof value.changed === "boolean" &&
    validDetail(value.application)
  );
}

async function read(path: string): Promise<unknown> {
  const response = await fetch(`${baseUrl()}${path}`, {
    cache: "no-store",
    signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
  });
  if (!response.ok) throw new Error("Application API request failed");
  return response.json();
}

/** Every tracked candidature of the configured profile. */
export async function getApplications(): Promise<ApplicationsResponse> {
  const payload = await read("/api/applications");
  if (!validListing(payload)) throw new Error("Application API response is invalid");
  return payload;
}

/** One candidature with its append-only timeline. */
export async function getApplication(id: number): Promise<ApplicationDetail> {
  const payload = await read(`/api/applications/${id}`);
  if (!validDetail(payload)) throw new Error("Application API response is invalid");
  return payload;
}

/** The listing, or null when the surface cannot be read right now. */
export async function loadApplications(): Promise<ApplicationsResponse | null> {
  try {
    return await getApplications();
  } catch {
    return null;
  }
}

/** One candidature, or null when it is absent or cannot be read. */
export async function loadApplication(
  id: number,
): Promise<ApplicationDetail | null> {
  try {
    return await getApplication(id);
  } catch {
    return null;
  }
}

async function forward(
  path: string,
  method: "POST" | "PATCH",
  body: unknown,
): Promise<ApplicationForwardResult> {
  let response: Response;
  try {
    response = await fetch(`${baseUrl()}${path}`, {
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
    // The backend's own class of failure is kept — a refused body stays a 4xx
    // and a business conflict stays a 409 — but its message is not relayed.
    return { ok: false, status: response.status };
  }
  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    return { ok: false, status: 502 };
  }
  return validWrite(payload)
    ? { ok: true, status: response.status, result: payload }
    : { ok: false, status: 502 };
}

/** Record the intention that makes a real opportunity a candidature. */
export function forwardCreate(body: unknown): Promise<ApplicationForwardResult> {
  return forward("/api/applications", "POST", body);
}

/** Set one candidature's status by hand. */
export function forwardStatus(
  id: number,
  body: unknown,
): Promise<ApplicationForwardResult> {
  return forward(`/api/applications/${id}/status`, "PATCH", body);
}

/** Write one candidature's manual tracking fields. */
export function forwardTracking(
  id: number,
  body: unknown,
): Promise<ApplicationForwardResult> {
  return forward(`/api/applications/${id}`, "PATCH", body);
}
