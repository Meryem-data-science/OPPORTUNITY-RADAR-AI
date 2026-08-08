import { describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));

import { createHealthResponse } from "./route";

describe("GET /api/health", () => {
  it("returns 200 when healthy", async () => {
    const response = await createHealthResponse(async () => ({
      status: "ok",
      database: "connected",
      schema: "ready",
      migrationVersion: "0001",
    }));

    expect(response.status).toBe(200);
  });

  it("returns 503 when unhealthy", async () => {
    const response = await createHealthResponse(async () => ({
      status: "error",
      database: "unavailable",
      schema: "unknown",
      migrationVersion: null,
    }));

    expect(response.status).toBe(503);
  });
});
