import { describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));

import { createPushConfigResponse } from "./response";

describe("GET /api/push/config", () => {
  it("relays a configured public key", async () => {
    const response = await createPushConfigResponse(async () => ({
      configured: true,
      vapid_public_key: "BPublicKey",
    }));

    expect(response.status).toBe(200);
    expect(response.headers.get("cache-control")).toBe("no-store");
    await expect(response.json()).resolves.toEqual({
      configured: true,
      vapid_public_key: "BPublicKey",
    });
  });

  it("reports an unconfigured server without inventing a key", async () => {
    const response = await createPushConfigResponse(async () => ({
      configured: false,
      vapid_public_key: null,
    }));

    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toEqual({
      configured: false,
      vapid_public_key: null,
    });
  });
});
