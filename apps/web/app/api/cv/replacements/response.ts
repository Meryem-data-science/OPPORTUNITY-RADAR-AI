import {
  forwardActivate,
  forwardCancel,
  forwardDecision,
  forwardOpen,
  forwardReady,
  getCurrentReview,
  type ActivationResult,
  type CancelResult,
  type ForwardResult,
  type ReviewSnapshot,
} from "@/lib/cv-replacement";

/**
 * The browser's only door to the CV replacement review.
 *
 * These handlers live on this app's own origin and forward to FastAPI, which
 * keeps the backend base URL a server value and makes a CORS configuration
 * unnecessary. They add no rules of their own: the backend decides what a
 * review may do, and this layer only decides how much of a refusal the browser
 * is told about.
 *
 * Two things they do enforce, because both are about what leaves this process
 * rather than about the review itself:
 *
 * * every response is `no-store`. These payloads carry CV readings, and a
 *   cached copy of one is a copy nobody asked for;
 * * `ready` and `cancel` take no body. A browser sending one is refused with a
 *   400 rather than having it quietly dropped — silently ignoring a field is
 *   how an attempt to smuggle one goes unnoticed.
 */
export const INVALID_REQUEST = "Requête de revue CV invalide.";
export const NOT_FOUND = "Revue de remplacement CV introuvable.";
export const CONFLICT = "La revue ne peut pas être modifiée ainsi.";
export const UNAVAILABLE = "La revue de remplacement CV est indisponible.";

const NO_STORE = { "cache-control": "no-store" } as const;

async function readJson(request: Request): Promise<unknown | undefined> {
  try {
    return await request.json();
  } catch {
    return undefined;
  }
}

/**
 * Report the class of failure and nothing else.
 *
 * The backend's own sentence never reaches the browser. It is built to hold no
 * reading, but this layer does not depend on that being true forever: each
 * class of refusal is answered with a fixed message of this app's own.
 */
export function failure(status: number): Response {
  const known: Record<number, [number, string]> = {
    400: [400, INVALID_REQUEST],
    404: [404, NOT_FOUND],
    409: [409, CONFLICT],
  };
  const [code, error] = known[status] ?? [503, UNAVAILABLE];
  return Response.json({ error }, { status: code, headers: NO_STORE });
}

function answer<T>(result: ForwardResult<T>, createdStatus = 200): Response {
  if (!result.ok) return failure(result.status);
  return Response.json(result.result, {
    status: result.status === 201 ? createdStatus : 200,
    headers: NO_STORE,
  });
}

/** A path segment is a replacement id only if it is a positive integer. */
export function replacementId(raw: string): number | null {
  return /^[1-9][0-9]{0,15}$/.test(raw) ? Number(raw) : null;
}

/** The open review, or the fixed unavailable answer. Never cached. */
export async function currentReviewResponse(
  read: () => Promise<ReviewSnapshot> = getCurrentReview,
): Promise<Response> {
  try {
    return Response.json(await read(), { headers: NO_STORE });
  } catch {
    return failure(503);
  }
}

export async function openReviewResponse(
  request: Request,
  send: (body: unknown) => Promise<ForwardResult<ReviewSnapshot>> = forwardOpen,
): Promise<Response> {
  const body = await readJson(request);
  if (body === undefined) return failure(400);
  return answer(await send(body), 201);
}

export async function decisionResponse(
  request: Request,
  raw: string,
  send: (
    id: number,
    body: unknown,
  ) => Promise<ForwardResult<ReviewSnapshot>> = forwardDecision,
): Promise<Response> {
  const id = replacementId(raw);
  if (id === null) return failure(400);
  const body = await readJson(request);
  if (body === undefined) return failure(400);
  return answer(await send(id, body));
}

/**
 * Whether this request carries a body on a route that takes none.
 *
 * Counted in **bytes received**, not in decoded characters. The distinction is
 * not academic: `text()` decodes as UTF-8 and strips a leading byte order
 * mark, so a body of exactly `EF BB BF` decodes to the empty string and would
 * have been waved through as no body at all. `arrayBuffer()` sees the three
 * bytes that were actually sent.
 *
 * Any byte counts, whitespace included. A body of spaces or newlines is still
 * a body: it is still something a client chose to send.
 *
 * A body that cannot be read counts as present. Assuming it was empty would
 * let one through precisely in the case where we could not see it, and the
 * cost of being wrong is a request reaching the backend unexamined.
 *
 * Only the byte count is ever looked at. The content is never decoded, parsed,
 * logged or echoed: a refused body may hold a correction somebody typed.
 */
async function carriesBody(request: Request): Promise<boolean> {
  try {
    return (await request.arrayBuffer()).byteLength > 0;
  } catch {
    return true;
  }
}

export async function readyResponse(
  request: Request,
  raw: string,
  send: (id: number) => Promise<ForwardResult<ReviewSnapshot>> = forwardReady,
): Promise<Response> {
  const id = replacementId(raw);
  if (id === null) return failure(400);
  if (await carriesBody(request)) return failure(400);
  return answer(await send(id));
}

export async function cancelResponse(
  request: Request,
  raw: string,
  send: (id: number) => Promise<ForwardResult<CancelResult>> = forwardCancel,
): Promise<Response> {
  const id = replacementId(raw);
  if (id === null) return failure(400);
  if (await carriesBody(request)) return failure(400);
  return answer(await send(id));
}

/**
 * Apply the reviewed replacement.
 *
 * The body is forwarded verbatim, so the `review_digest` that reaches the
 * backend is the one the person confirmed. Nothing here reads the stored token
 * to fill it in: that would turn the confirmation into a formality and let a
 * review that moved since be applied anyway.
 */
export async function activateResponse(
  request: Request,
  raw: string,
  send: (
    id: number,
    body: unknown,
  ) => Promise<ForwardResult<ActivationResult>> = forwardActivate,
): Promise<Response> {
  const id = replacementId(raw);
  if (id === null) return failure(400);
  const body = await readJson(request);
  if (body === undefined) return failure(400);
  return answer(await send(id, body));
}
