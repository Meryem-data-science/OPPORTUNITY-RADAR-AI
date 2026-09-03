import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));

import { forwardSubscribe, forwardUnsubscribe, getPushConfig } from "./push";

const BODY = {
  endpoint: "https://push.example.invalid/subscription/abc",
  keys: { p256dh: "BNkey", auth: "authsecret" },
};

function json(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  delete process.env.OPPORTUNITY_API_BASE_URL;
});

afterEach(() => {
  vi.unstubAllGlobals();
  delete process.env.OPPORTUNITY_API_BASE_URL;
});

describe("push backend proxy", () => {
  it("calls the server-configured backend, never a public one", async () => {
    process.env.OPPORTUNITY_API_BASE_URL = "http://backend.invalid:8000/";
    fetchMock.mockResolvedValueOnce(json({ configured: false, vapid_public_key: null }));

    await getPushConfig();

    expect(fetchMock.mock.calls[0][0]).toBe("http://backend.invalid:8000/api/push/config");
    expect(
      Object.keys(process.env).filter((name) => name.startsWith("NEXT_PUBLIC")),
    ).toEqual([]);
  });

  it("defaults to the loopback backend", async () => {
    fetchMock.mockResolvedValueOnce(json({ configured: false, vapid_public_key: null }));

    await getPushConfig();

    expect(fetchMock.mock.calls[0][0]).toBe("http://127.0.0.1:8000/api/push/config");
  });

  it("relays a valid configuration", async () => {
    fetchMock.mockResolvedValueOnce(json({ configured: true, vapid_public_key: "BKey" }));

    await expect(getPushConfig()).resolves.toEqual({
      configured: true,
      vapid_public_key: "BKey",
    });
  });

  it.each([
    ["an unreachable backend", () => Promise.reject(new Error("down"))],
    ["an error status", () => Promise.resolve(json({}, 503))],
    ["a malformed payload", () => Promise.resolve(json({ configured: true, vapid_public_key: null }))],
    ["a non-object payload", () => Promise.resolve(json("nope"))],
  ])("fails closed on %s", async (_label, behaviour) => {
    fetchMock.mockImplementationOnce(behaviour);

    await expect(getPushConfig()).resolves.toEqual({
      configured: false,
      vapid_public_key: null,
    });
  });

  it("forwards a subscribe as a POST and returns the backend state", async () => {
    fetchMock.mockResolvedValueOnce(json({ status: "ACTIVE" }, 201));

    await expect(forwardSubscribe(BODY)).resolves.toEqual({
      ok: true,
      state: { status: "ACTIVE" },
    });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://127.0.0.1:8000/api/push/subscriptions");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body)).toEqual(BODY);
  });

  it("forwards an unsubscribe as a DELETE", async () => {
    fetchMock.mockResolvedValueOnce(json({ status: "REVOKED" }));

    await expect(forwardUnsubscribe({ endpoint: BODY.endpoint })).resolves.toEqual({
      ok: true,
      state: { status: "REVOKED" },
    });
    expect(fetchMock.mock.calls[0][1].method).toBe("DELETE");
  });

  it("keeps a backend refusal a client error and an outage a 503", async () => {
    fetchMock.mockResolvedValueOnce(json({ detail: "leaky backend detail" }, 400));
    await expect(forwardSubscribe(BODY)).resolves.toEqual({ ok: false, status: 400 });

    fetchMock.mockRejectedValueOnce(new Error("down"));
    await expect(forwardSubscribe(BODY)).resolves.toEqual({ ok: false, status: 503 });
  });

  it("refuses a backend answer that is not a known state", async () => {
    fetchMock.mockResolvedValueOnce(json({ status: "MAYBE" }));

    await expect(forwardSubscribe(BODY)).resolves.toEqual({ ok: false, status: 502 });
  });
});
