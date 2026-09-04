import {
  forwardCreate,
  forwardStatus,
  forwardTracking,
  type ApplicationForwardResult,
} from "@/lib/applications";

/**
 * The browser's only door to application tracking.
 *
 * These handlers live on this app's own origin and forward to FastAPI, which
 * keeps the backend base URL a server value and makes a CORS configuration
 * unnecessary. They add no rules of their own: the backend decides what a
 * candidature may do, and this layer only decides how much of a refusal the
 * browser is told about.
 */
export const INVALID_REQUEST = "Application request is invalid.";
export const NOT_FOUND = "Application not found.";
export const CONFLICT = "Application cannot be changed this way.";
export const UNAVAILABLE = "Application tracking is temporarily unavailable.";

export type Forward = (body: unknown) => Promise<ApplicationForwardResult>;

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
 * The backend's own sentence never reaches the browser — a private note could
 * never end up in one, but a database path or a SQL message might — so each
 * class of refusal is answered with a fixed message of this app's own.
 */
function failure(status: number): Response {
  const known: Record<number, [number, string]> = {
    400: [400, INVALID_REQUEST],
    404: [404, NOT_FOUND],
    409: [409, CONFLICT],
  };
  const [code, error] = known[status] ?? [503, UNAVAILABLE];
  return Response.json({ error }, { status: code, headers: { "cache-control": "no-store" } });
}

async function proxy(request: Request, send: Forward): Promise<Response> {
  const body = await readJson(request);
  if (body === undefined) return failure(400);
  const result = await send(body);
  if (!result.ok) return failure(result.status);
  return Response.json(result.result, {
    status: result.status === 201 ? 201 : 200,
    headers: { "cache-control": "no-store" },
  });
}

/** A path segment is an application id only if it is a positive integer. */
export function applicationId(raw: string): number | null {
  return /^[1-9][0-9]{0,15}$/.test(raw) ? Number(raw) : null;
}

export function createApplicationResponse(
  request: Request,
  send: Forward = forwardCreate,
): Promise<Response> {
  return proxy(request, send);
}

export function updateStatusResponse(
  request: Request,
  raw: string,
  send: (id: number, body: unknown) => Promise<ApplicationForwardResult> = forwardStatus,
): Promise<Response> {
  const id = applicationId(raw);
  if (id === null) return Promise.resolve(failure(400));
  return proxy(request, (body) => send(id, body));
}

export function updateTrackingResponse(
  request: Request,
  raw: string,
  send: (id: number, body: unknown) => Promise<ApplicationForwardResult> = forwardTracking,
): Promise<Response> {
  const id = applicationId(raw);
  if (id === null) return Promise.resolve(failure(400));
  return proxy(request, (body) => send(id, body));
}
