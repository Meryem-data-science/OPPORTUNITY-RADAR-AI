import { describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));

import {
  CONFLICT,
  INVALID_REQUEST,
  NOT_FOUND,
  UNAVAILABLE,
  activateResponse,
  cancelResponse,
  currentReviewResponse,
  decisionResponse,
  openReviewResponse,
  readyResponse,
  replacementId,
} from "./response";
import {
  READY_DIGEST,
  SENTINEL_CORRECTION,
  SENTINEL_READING,
  activation,
  noReview,
  openReview,
  readyReview,
} from "@/lib/cv-replacement.fixture";

const CANCELLED = { replacement_id: 7, effective_state: "CANCELLED" as const };

function request(body?: unknown, method = "POST"): Request {
  return new Request("https://app.example.invalid/api/cv/replacements", {
    method,
    ...(body === undefined
      ? {}
      : {
          headers: { "content-type": "application/json" },
          body: typeof body === "string" ? body : JSON.stringify(body),
        }),
  });
}

describe("GET /api/cv/replacements", () => {
  it("returns the review and forbids caching it", async () => {
    const response = await currentReviewResponse(async () => openReview);

    expect(response.status).toBe(200);
    expect(response.headers.get("cache-control")).toBe("no-store");
    await expect(response.json()).resolves.toEqual(openReview);
  });

  it("reports the absence of a review without inventing one", async () => {
    const response = await currentReviewResponse(async () => noReview);

    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toMatchObject({ replacement: null });
  });

  it("answers a backend that cannot be read with a fixed sentence", async () => {
    const response = await currentReviewResponse(async () => {
      throw new Error("schema is not ready; migrate through 0028");
    });

    expect(response.status).toBe(503);
    const payload = await response.json();
    expect(payload).toEqual({ error: UNAVAILABLE });
    // Neither the backend's wording nor anything about migrations leaks.
    expect(JSON.stringify(payload)).not.toContain("0028");
  });
});

describe("POST /api/cv/replacements", () => {
  it("forwards the body verbatim and answers 201", async () => {
    const send = vi.fn(async () => ({
      ok: true as const,
      status: 201,
      result: openReview,
    }));

    const response = await openReviewResponse(
      request({ extraction_id: 3 }),
      send,
    );

    expect(send).toHaveBeenCalledWith({ extraction_id: 3 });
    expect(response.status).toBe(201);
    expect(response.headers.get("cache-control")).toBe("no-store");
  });

  it("refuses a body that is not JSON without calling the backend", async () => {
    const send = vi.fn();
    const response = await openReviewResponse(request("{not json"), send);

    expect(send).not.toHaveBeenCalled();
    expect(response.status).toBe(400);
    await expect(response.json()).resolves.toEqual({ error: INVALID_REQUEST });
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

    const response = await openReviewResponse(request({ extraction_id: 3 }), send);

    expect(response.status).toBe(status === 400 || status === 404 || status === 409 ? status : 503);
    await expect(response.json()).resolves.toEqual({ error });
  });
});

describe("PUT /api/cv/replacements/[id]/decision", () => {
  it("forwards the decision, correction included, against the parsed id", async () => {
    const send = vi.fn(async () => ({
      ok: true as const,
      status: 200,
      result: openReview,
    }));
    const body = {
      candidate_id: 11,
      decision: "CORRECT",
      staged_value: `  ${SENTINEL_CORRECTION}  `,
    };

    const response = await decisionResponse(request(body, "PUT"), "7", send);

    // Byte for byte: no trim, nothing normalized on the way through.
    expect(send).toHaveBeenCalledWith(7, body);
    expect(response.status).toBe(200);
  });

  it.each(["0", "-1", "1.5", "abc", "", " 7", "07", "9".repeat(17)])(
    "refuses %o as a replacement id without calling the backend",
    async (raw) => {
      const send = vi.fn();
      const response = await decisionResponse(request({}, "PUT"), raw, send);

      expect(send).not.toHaveBeenCalled();
      expect(response.status).toBe(400);
      expect(replacementId(raw)).toBeNull();
    },
  );

  it("reports a decision the domain forbids as a conflict", async () => {
    const send = vi.fn(async () => ({ ok: false as const, status: 409 }));

    const response = await decisionResponse(
      request({ fact_id: 22, decision: "RETIRE" }, "PUT"),
      "7",
      send,
    );

    expect(response.status).toBe(409);
    await expect(response.json()).resolves.toEqual({ error: CONFLICT });
  });
});

describe("POST /api/cv/replacements/[id]/ready and /cancel", () => {
  it("call the backend with no body when the request has none", async () => {
    const ready = vi.fn(async () => ({
      ok: true as const,
      status: 200,
      result: readyReview,
    }));
    const cancel = vi.fn(async () => ({
      ok: true as const,
      status: 200,
      result: CANCELLED,
    }));

    const readyResult = await readyResponse(request(), "7", ready);
    const cancelResult = await cancelResponse(request(), "7", cancel);

    expect(ready).toHaveBeenCalledWith(7);
    expect(cancel).toHaveBeenCalledWith(7);
    expect(readyResult.status).toBe(200);
    expect(cancelResult.status).toBe(200);
    await expect(readyResult.json()).resolves.toMatchObject({
      replacement: { ready_review_digest: READY_DIGEST },
    });
  });

  it.each([
    ["a profile id", { profile_id: 99 }],
    ["an unknown field", { unexpected: "x" }],
    ["an empty object", {}],
    ["a correction", { staged_value: SENTINEL_CORRECTION }],
  ])("refuse %s rather than dropping it silently", async (_label, body) => {
    const ready = vi.fn();
    const cancel = vi.fn();

    const readyResult = await readyResponse(request(body), "7", ready);
    const cancelResult = await cancelResponse(request(body), "7", cancel);

    // Refused, not ignored: the backend is never called at all.
    expect(ready).not.toHaveBeenCalled();
    expect(cancel).not.toHaveBeenCalled();
    expect(readyResult.status).toBe(400);
    expect(cancelResult.status).toBe(400);
    const payload = await readyResult.json();
    expect(payload).toEqual({ error: INVALID_REQUEST });
    // Nothing the refused body contained is echoed back.
    expect(JSON.stringify(payload)).not.toContain(SENTINEL_CORRECTION);
    expect(JSON.stringify(payload)).not.toContain("profile_id");
  });
});

describe("POST /api/cv/replacements/[id]/activate", () => {
  it("passes the confirmed digest through untouched", async () => {
    const send = vi.fn(async () => ({
      ok: true as const,
      status: 200,
      result: activation,
    }));

    const response = await activateResponse(
      request({ review_digest: READY_DIGEST }),
      "7",
      send,
    );

    expect(send).toHaveBeenCalledWith(7, { review_digest: READY_DIGEST });
    expect(response.status).toBe(200);
    const payload = await response.json();
    expect(payload).toEqual(activation);
    // Counters only: not one CV reading in the activation answer.
    expect(JSON.stringify(payload)).not.toContain(SENTINEL_READING);
  });

  it("reports a stale confirmation as a conflict, and applies nothing", async () => {
    const send = vi.fn(async () => ({ ok: false as const, status: 409 }));

    const response = await activateResponse(
      request({ review_digest: READY_DIGEST }),
      "7",
      send,
    );

    expect(send).toHaveBeenCalledTimes(1); // No automatic retry.
    expect(response.status).toBe(409);
    await expect(response.json()).resolves.toEqual({ error: CONFLICT });
  });

  it("refuses a request with no usable body", async () => {
    const send = vi.fn();
    const response = await activateResponse(request("{"), "7", send);

    expect(send).not.toHaveBeenCalled();
    expect(response.status).toBe(400);
  });
});


describe("ready and cancel refuse any body at all", () => {
  function raw(body: string): Request {
    // A body of literal bytes, whatever they are.
    return new Request("https://app.example.invalid/api/cv/replacements/7/ready", {
      method: "POST",
      body,
    });
  }

  it("lets a request with no body through unchanged", async () => {
    const ready = vi.fn(async () => ({
      ok: true as const,
      status: 200,
      result: readyReview,
    }));
    const cancel = vi.fn(async () => ({
      ok: true as const,
      status: 200,
      result: CANCELLED,
    }));

    const withNoBody = new Request("https://app.example.invalid/x", {
      method: "POST",
    });

    expect((await readyResponse(withNoBody, "7", ready)).status).toBe(200);
    expect(
      (
        await cancelResponse(
          new Request("https://app.example.invalid/x", { method: "POST" }),
          "7",
          cancel,
        )
      ).status,
    ).toBe(200);
    expect(ready).toHaveBeenCalledWith(7);
    expect(cancel).toHaveBeenCalledWith(7);
  });

  it.each([
    ["a single space", " "],
    ["several spaces", "    "],
    ["a tab", "\t"],
    ["newlines only", "\n\n"],
    ["mixed whitespace", " \t\r\n "],
    ["a single byte", "x"],
    ["an empty JSON object", "{}"],
  ])("refuses %s without calling the backend", async (_label, body) => {
    const ready = vi.fn();
    const cancel = vi.fn();

    const readyResult = await readyResponse(raw(body), "7", ready);
    const cancelResult = await cancelResponse(raw(body), "7", cancel);

    expect(ready).not.toHaveBeenCalled();
    expect(cancel).not.toHaveBeenCalled();
    expect(readyResult.status).toBe(400);
    expect(cancelResult.status).toBe(400);
    await expect(readyResult.json()).resolves.toEqual({ error: INVALID_REQUEST });
  });

  it("refuses a body of bytes that decode to nothing", async () => {
    // EF BB BF is a UTF-8 byte order mark. `text()` strips it and returns the
    // empty string, so a check on decoded characters would have called this no
    // body at all. These are three bytes a client chose to send.
    const bom = () =>
      new Request("https://app.example.invalid/api/cv/replacements/7/ready", {
        method: "POST",
        body: Uint8Array.from([0xef, 0xbb, 0xbf]),
      });

    // The premise, asserted rather than assumed.
    expect(await bom().text()).toHaveLength(0);
    expect((await bom().arrayBuffer()).byteLength).toBe(3);

    const ready = vi.fn();
    const cancel = vi.fn();
    const readyResult = await readyResponse(bom(), "7", ready);
    const cancelResult = await cancelResponse(bom(), "7", cancel);

    expect(ready).not.toHaveBeenCalled();
    expect(cancel).not.toHaveBeenCalled();
    expect(readyResult.status).toBe(400);
    expect(cancelResult.status).toBe(400);
    for (const result of [readyResult, cancelResult]) {
      const payload = await result.json();
      expect(payload).toEqual({ error: INVALID_REQUEST });
      // Nothing of the body, decoded or raw, comes back.
      expect(JSON.stringify(payload)).not.toContain("\ufeff");
      expect(JSON.stringify(payload)).not.toContain("efbbbf");
    }
  });

  it("refuses a body it cannot read rather than assuming it was empty", async () => {
    const ready = vi.fn();
    const cancel = vi.fn();
    const unreadable = () =>
      ({
        method: "POST",
        text: async () => {
          throw new Error("stream broke");
        },
      }) as unknown as Request;

    const readyResult = await readyResponse(unreadable(), "7", ready);
    const cancelResult = await cancelResponse(unreadable(), "7", cancel);

    expect(ready).not.toHaveBeenCalled();
    expect(cancel).not.toHaveBeenCalled();
    expect(readyResult.status).toBe(400);
    expect(cancelResult.status).toBe(400);
  });

  it("echoes nothing the refused body contained", async () => {
    const body = JSON.stringify({
      profile_id: 99,
      staged_value: SENTINEL_CORRECTION,
    });

    const response = await readyResponse(raw(body), "7", vi.fn());
    const payload = await response.json();

    expect(payload).toEqual({ error: INVALID_REQUEST });
    const serialized = JSON.stringify(payload);
    expect(serialized).not.toContain(SENTINEL_CORRECTION);
    expect(serialized).not.toContain("profile_id");
    expect(serialized).not.toContain("99");
  });
});
