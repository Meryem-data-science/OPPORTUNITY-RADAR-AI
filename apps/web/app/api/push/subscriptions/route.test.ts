import { describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));

import {
  INVALID_REQUEST,
  UNAVAILABLE,
  createSubscribeResponse,
  createUnsubscribeResponse,
} from "./response";

const BODY = {
  endpoint: "https://push.example.invalid/subscription/abc",
  keys: { p256dh: "BNkey", auth: "authsecret" },
};

function request(body: unknown, method = "POST"): Request {
  return new Request("https://app.example.invalid/api/push/subscriptions", {
    method,
    headers: { "content-type": "application/json" },
    body: typeof body === "string" ? body : JSON.stringify(body),
  });
}

describe("POST /api/push/subscriptions", () => {
  it("forwards the body verbatim and answers 201", async () => {
    const send = vi.fn(async () => ({ ok: true as const, state: { status: "ACTIVE" as const } }));

    const response = await createSubscribeResponse(request(BODY), send);

    expect(send).toHaveBeenCalledWith(BODY);
    expect(response.status).toBe(201);
    await expect(response.json()).resolves.toEqual({ status: "ACTIVE" });
  });

  it("reports a refused body as a generic 400 and never relays a backend message", async () => {
    const send = vi.fn(async () => ({ ok: false as const, status: 409 }));

    const response = await createSubscribeResponse(request(BODY), send);

    expect(response.status).toBe(400);
    await expect(response.json()).resolves.toEqual({ error: INVALID_REQUEST });
  });

  it("reports a backend outage as 503", async () => {
    const send = vi.fn(async () => ({ ok: false as const, status: 503 }));

    const response = await createSubscribeResponse(request(BODY), send);

    expect(response.status).toBe(503);
    await expect(response.json()).resolves.toEqual({ error: UNAVAILABLE });
  });

  it("refuses a body that is not JSON without calling the backend", async () => {
    const send = vi.fn();

    const response = await createSubscribeResponse(request("{not json"), send);

    expect(send).not.toHaveBeenCalled();
    expect(response.status).toBe(400);
    await expect(response.json()).resolves.toEqual({ error: INVALID_REQUEST });
  });
});

describe("DELETE /api/push/subscriptions", () => {
  it("forwards the endpoint and answers 200", async () => {
    const send = vi.fn(async () => ({ ok: true as const, state: { status: "REVOKED" as const } }));

    const response = await createUnsubscribeResponse(
      request({ endpoint: BODY.endpoint }, "DELETE"),
      send,
    );

    expect(send).toHaveBeenCalledWith({ endpoint: BODY.endpoint });
    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toEqual({ status: "REVOKED" });
  });
});
