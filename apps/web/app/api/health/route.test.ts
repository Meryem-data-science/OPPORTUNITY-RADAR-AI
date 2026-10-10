import { describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));

import { createHealthResponse } from "./response";

describe("GET /api/health", () => {
  it("returns 200 when healthy", async () => {
    const response = await createHealthResponse(async () => ({
      status: "ok",
      api: "available",
      databaseBackend: "sqlite",
      database: "connected",
      schema: "ready",
      migrationVersion: "0026",
      minimumMigrationVersion: "0026",
      profile: "ready",
      reason: null,
    }));

    expect(response.status).toBe(200);
  });

  it("returns 503 when unhealthy", async () => {
    const response = await createHealthResponse(async () => ({
      status: "error",
      api: "unavailable",
      databaseBackend: "unknown",
      database: "unavailable",
      schema: "unknown",
      migrationVersion: null,
      minimumMigrationVersion: "0026",
      profile: "unknown",
      reason: "API_UNAVAILABLE",
    }));

    expect(response.status).toBe(503);
  });
});
