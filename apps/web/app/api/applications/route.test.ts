import { describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));

import {
  CONFLICT,
  INVALID_REQUEST,
  NOT_FOUND,
  UNAVAILABLE,
  applicationId,
  createApplicationResponse,
  updateStatusResponse,
  updateTrackingResponse,
} from "./response";
import { trackedDetail } from "@/lib/applications.fixture";

const CREATED = { created: true, changed: true, application: trackedDetail };
const UNCHANGED = { created: false, changed: false, application: trackedDetail };

function request(body: unknown, method = "POST"): Request {
  return new Request("https://app.example.invalid/api/applications", {
    method,
    headers: { "content-type": "application/json" },
    body: typeof body === "string" ? body : JSON.stringify(body),
  });
}

describe("POST /api/applications", () => {
  it("forwards the body verbatim and answers 201 for a new candidature", async () => {
    const send = vi.fn(async () => ({ ok: true as const, status: 201, result: CREATED }));
    const body = { opportunity_id: 42, action: "SAVE" };

    const response = await createApplicationResponse(request(body), send);

    expect(send).toHaveBeenCalledWith(body);
    expect(response.status).toBe(201);
    await expect(response.json()).resolves.toEqual(CREATED);
  });

  it("answers 200 when the candidature already existed", async () => {
    const send = vi.fn(async () => ({
      ok: true as const,
      status: 200,
      result: UNCHANGED,
    }));

    const response = await createApplicationResponse(
      request({ opportunity_id: 42, action: "SAVE" }),
      send,
    );

    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toMatchObject({ changed: false });
  });

  it.each([
    [400, INVALID_REQUEST],
    [404, NOT_FOUND],
    [409, CONFLICT],
    [503, UNAVAILABLE],
    [500, UNAVAILABLE],
    [502, UNAVAILABLE],
  ])("reports a %i without relaying the backend message", async (status, error) => {
    const send = vi.fn(async () => ({ ok: false as const, status }));

    const response = await createApplicationResponse(request({}), send);

    expect(response.status).toBe(status === 500 || status === 502 ? 503 : status);
    await expect(response.json()).resolves.toEqual({ error });
  });

  it("refuses a body that is not JSON without calling the backend", async () => {
    const send = vi.fn();

    const response = await createApplicationResponse(request("{not json"), send);

    expect(send).not.toHaveBeenCalled();
    expect(response.status).toBe(400);
  });
});

describe("PATCH /api/applications/[id]", () => {
  it("forwards the tracking body against the parsed id", async () => {
    const send = vi.fn(async () => ({
      ok: true as const,
      status: 200,
      result: UNCHANGED,
    }));

    const response = await updateTrackingResponse(
      request({ notes: null }, "PATCH"),
      "7",
      send,
    );

    expect(send).toHaveBeenCalledWith(7, { notes: null });
    expect(response.status).toBe(200);
  });

  it("forwards the status body against the parsed id", async () => {
    const send = vi.fn(async () => ({
      ok: true as const,
      status: 200,
      result: UNCHANGED,
    }));

    await updateStatusResponse(request({ status: "INTERVIEW" }, "PATCH"), "7", send);

    expect(send).toHaveBeenCalledWith(7, { status: "INTERVIEW" });
  });

  it.each(["0", "-1", "1.5", "abc", "", " 7", "07", "9".repeat(17)])(
    "refuses %o as an application id without calling the backend",
    async (raw) => {
      const send = vi.fn();

      const status = await updateStatusResponse(request({}, "PATCH"), raw, send);
      const tracking = await updateTrackingResponse(request({}, "PATCH"), raw, send);

      expect(send).not.toHaveBeenCalled();
      expect(status.status).toBe(400);
      expect(tracking.status).toBe(400);
      expect(applicationId(raw)).toBeNull();
    },
  );

  it("reports a business conflict as 409", async () => {
    const send = vi.fn(async () => ({ ok: false as const, status: 409 }));

    const response = await updateStatusResponse(
      request({ status: "SAVED" }, "PATCH"),
      "7",
      send,
    );

    expect(response.status).toBe(409);
    await expect(response.json()).resolves.toEqual({ error: CONFLICT });
  });
});
