import {
  forwardSubscribe,
  forwardUnsubscribe,
  type PushForwardResult,
} from "@/lib/push";

export const INVALID_REQUEST = "Push subscription request is invalid.";
export const UNAVAILABLE = "Push subscriptions are temporarily unavailable.";

type Forward = (body: unknown) => Promise<PushForwardResult>;

async function readJson(request: Request): Promise<unknown | undefined> {
  try {
    return await request.json();
  } catch {
    return undefined;
  }
}

function failure(status: number): Response {
  // Anything the backend refused is reported as a plain refusal, and anything
  // else as unavailable: no backend message ever reaches the browser.
  const clientError = status >= 400 && status < 500;
  return Response.json(
    { error: clientError ? INVALID_REQUEST : UNAVAILABLE },
    { status: clientError ? 400 : 503, headers: { "cache-control": "no-store" } },
  );
}

async function proxy(
  request: Request,
  send: Forward,
  successStatus: number,
): Promise<Response> {
  const body = await readJson(request);
  if (body === undefined) return failure(400);
  const result = await send(body);
  if (!result.ok) return failure(result.status);
  return Response.json(result.state, {
    status: successStatus,
    headers: { "cache-control": "no-store" },
  });
}

export function createSubscribeResponse(
  request: Request,
  send: Forward = forwardSubscribe,
): Promise<Response> {
  return proxy(request, send, 201);
}

export function createUnsubscribeResponse(
  request: Request,
  send: Forward = forwardUnsubscribe,
): Promise<Response> {
  return proxy(request, send, 200);
}
