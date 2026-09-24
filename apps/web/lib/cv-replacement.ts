import "server-only";

/**
 * Server-side access to the Python CV replacement surface.
 *
 * Every read and every write goes through FastAPI. This module runs on the
 * server, the backend base URL never becomes a NEXT_PUBLIC_ variable, and
 * nothing here — or in the browser — opens SQLite: the backend is the only
 * thing that touches the review, because it is the only thing that knows the
 * rules one obeys.
 *
 * The profile is not passed. The backend resolves the single configured
 * profile itself, so a page cannot ask about somebody else's CV.
 *
 * **These responses carry CV readings.** They are the one thing the person is
 * here to examine, so they reach the page — and nothing else. Nothing in this
 * module logs a payload, puts a reading in a URL or a message, or hands one to
 * anything but the caller. A failure is reported by its HTTP class alone; the
 * backend's own sentence is never relayed, because it is the one nobody has
 * checked for what it might contain.
 */
const API_BASE = "http://127.0.0.1:8000";
const REQUEST_TIMEOUT_MS = 5_000;

export * from "./cv-replacement-contract";

import {
  DIFFERENCES,
  EFFECTIVE_STATES,
  EXISTING_DECISIONS,
  INCOMING_DECISIONS,
  type ActivationResult,
  type CancelResult,
  type ReviewEntry,
  type ReviewSnapshot,
} from "./cv-replacement-contract";

/** What a forwarded write produced, or the class of refusal it met. */
export type ForwardResult<T> =
  | { ok: true; status: number; result: T }
  | { ok: false; status: number };

const object = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const identifier = (value: unknown): value is number =>
  typeof value === "number" && Number.isInteger(value) && value > 0;

const counter = (value: unknown): value is number =>
  typeof value === "number" && Number.isInteger(value) && value >= 0;

const text = (value: unknown): value is string => typeof value === "string";

const optionalText = (value: unknown): value is string | null =>
  value === null || typeof value === "string";

const optionalIdentifier = (value: unknown): value is number | null =>
  value === null || identifier(value);

const member = (value: unknown, allowed: readonly string[]): boolean =>
  typeof value === "string" && allowed.includes(value);

function baseUrl(): string {
  return (process.env.OPPORTUNITY_API_BASE_URL ?? API_BASE).replace(/\/$/, "");
}

function validReplacement(value: unknown): boolean {
  if (!object(value)) return false;
  return (
    identifier(value.replacement_id) &&
    identifier(value.extraction_id) &&
    optionalIdentifier(value.baseline_document_id) &&
    text(value.lifecycle) &&
    member(value.effective_state, EFFECTIVE_STATES) &&
    // A token is 64 lowercase hex characters or nothing at all. A malformed
    // one is refused here rather than sent back as a confirmation.
    (value.ready_review_digest === null ||
      (text(value.ready_review_digest) &&
        /^[0-9a-f]{64}$/.test(value.ready_review_digest))) &&
    optionalIdentifier(value.activation_revision) &&
    optionalText(value.activated_at)
  );
}

function validProgress(value: unknown): boolean {
  if (!object(value)) return false;
  return (
    counter(value.plan_entries) &&
    counter(value.answered) &&
    counter(value.unanswered) &&
    counter(value.decisions_outside_plan) &&
    counter(value.stale_decisions) &&
    object(value.counts_by_difference) &&
    Object.values(value.counts_by_difference).every(counter)
  );
}

function validEntry(value: unknown): value is ReviewEntry {
  if (!object(value)) return false;
  const incoming = value.role === "INCOMING";
  return (
    (incoming || value.role === "EXISTING") &&
    member(value.difference, DIFFERENCES) &&
    // Exactly one target, decided by the role: the backend guarantees it, and
    // a payload that disagrees is not one this page will render.
    (incoming
      ? identifier(value.candidate_id) && value.fact_id === null
      : identifier(value.fact_id) && value.candidate_id === null) &&
    text(value.fact_type) &&
    optionalText(value.fact_status) &&
    text(value.display_value) &&
    (value.decision === null ||
      member(
        value.decision,
        incoming ? INCOMING_DECISIONS : EXISTING_DECISIONS,
      )) &&
    typeof value.has_staged_value === "boolean"
  );
}

function validSnapshot(value: unknown): value is ReviewSnapshot {
  if (!object(value)) return false;
  if (!Array.isArray(value.entries) || !value.entries.every(validEntry)) {
    return false;
  }
  if (value.replacement === null) {
    // No open review: there is nothing to describe, and the backend says so by
    // sending nothing rather than an empty shell.
    return value.progress === null && value.entries.length === 0;
  }
  return (
    validReplacement(value.replacement) &&
    validProgress(value.progress) &&
    (value.progress as { plan_entries: number }).plan_entries ===
      value.entries.length
  );
}

function validActivation(value: unknown): value is ActivationResult {
  if (!object(value)) return false;
  return (
    identifier(value.replacement_id) &&
    typeof value.activated === "boolean" &&
    counter(value.activation_revision) &&
    optionalIdentifier(value.previous_document_id) &&
    identifier(value.document_id) &&
    counter(value.facts_accepted) &&
    counter(value.facts_rejected) &&
    counter(value.facts_corrected) &&
    counter(value.facts_retired) &&
    counter(value.evidence_attached) &&
    counter(value.decisions_skipped) &&
    member(value.effective_state, EFFECTIVE_STATES)
  );
}

function validCancel(value: unknown): value is CancelResult {
  if (!object(value)) return false;
  return (
    identifier(value.replacement_id) && member(value.effective_state, EFFECTIVE_STATES)
  );
}

/** The open review of the configured profile. Never cached. */
export async function getCurrentReview(): Promise<ReviewSnapshot> {
  const response = await fetch(`${baseUrl()}/api/cv/replacements/current`, {
    cache: "no-store",
    signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
  });
  if (!response.ok) throw new Error("CV replacement API request failed");
  const payload: unknown = await response.json();
  if (!validSnapshot(payload)) {
    throw new Error("CV replacement API response is invalid");
  }
  return payload;
}

/**
 * The open review, or null when the surface cannot be read right now.
 *
 * Null is the honest answer to a backend that is down, misconfigured, or
 * running against a database this workflow's migrations have not reached. The
 * page says so; it never invents a review to have something to show.
 */
export async function loadCurrentReview(): Promise<ReviewSnapshot | null> {
  try {
    return await getCurrentReview();
  } catch {
    return null;
  }
}

async function forward<T>(
  path: string,
  method: "POST" | "PUT",
  body: unknown | undefined,
  valid: (value: unknown) => value is T,
): Promise<ForwardResult<T>> {
  let response: Response;
  try {
    response = await fetch(`${baseUrl()}${path}`, {
      method,
      cache: "no-store",
      // `ready` and `cancel` take no body at all, and the backend refuses one.
      // Sending an empty object "just in case" would be exactly the request it
      // is built to reject, so nothing is sent.
      ...(body === undefined
        ? {}
        : {
            headers: { "content-type": "application/json" },
            body: JSON.stringify(body),
          }),
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    });
  } catch {
    return { ok: false, status: 503 };
  }
  if (!response.ok) {
    // The backend's class of failure is kept — a refused body stays a 400, a
    // business conflict stays a 409 — and its message is not relayed.
    return { ok: false, status: response.status };
  }
  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    return { ok: false, status: 502 };
  }
  return valid(payload)
    ? { ok: true, status: response.status, result: payload }
    : { ok: false, status: 502 };
}

/** Open a review over an extraction this profile already holds. */
export function forwardOpen(body: unknown): Promise<ForwardResult<ReviewSnapshot>> {
  return forward("/api/cv/replacements", "POST", body, validSnapshot);
}

/** Record or replace one human answer. */
export function forwardDecision(
  id: number,
  body: unknown,
): Promise<ForwardResult<ReviewSnapshot>> {
  return forward(
    `/api/cv/replacements/${id}/decision`,
    "PUT",
    body,
    validSnapshot,
  );
}

/** Declare the review ready. No body: the backend refuses one. */
export function forwardReady(id: number): Promise<ForwardResult<ReviewSnapshot>> {
  return forward(
    `/api/cv/replacements/${id}/ready`,
    "POST",
    undefined,
    validSnapshot,
  );
}

/** Apply the reviewed replacement, against the token the person confirmed. */
export function forwardActivate(
  id: number,
  body: unknown,
): Promise<ForwardResult<ActivationResult>> {
  return forward(
    `/api/cv/replacements/${id}/activate`,
    "POST",
    body,
    validActivation,
  );
}

/** Abandon an open review. No body: the backend refuses one. */
export function forwardCancel(id: number): Promise<ForwardResult<CancelResult>> {
  return forward(
    `/api/cv/replacements/${id}/cancel`,
    "POST",
    undefined,
    validCancel,
  );
}
