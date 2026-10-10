import { describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));

import { getSystemHealth, type SystemHealth } from "./health";

const healthy: SystemHealth = {
  status: "ok",
  api: "available",
  databaseBackend: "sqlite",
  database: "connected",
  schema: "ready",
  migrationVersion: "0026",
  minimumMigrationVersion: "0026",
  profile: "ready",
  reason: null,
};

function fetchWithResponse(payload: SystemHealth, status = 200): typeof fetch {
  return vi.fn(async () => Response.json(payload, { status })) as typeof fetch;
}

describe("getSystemHealth", () => {
  it("reports the API's connected database and operational schema", async () => {
    await expect(getSystemHealth(fetchWithResponse(healthy))).resolves.toEqual(healthy);
  });

  it("reports unavailable services when the API cannot be reached", async () => {
    const fetchHealth = vi.fn(async () => {
      throw new Error("connection failed");
    }) as typeof fetch;

    expect(await getSystemHealth(fetchHealth)).toMatchObject({
      status: "error",
      api: "unavailable",
      database: "unavailable",
    });
  });

  it("reports the API's degraded schema when a required table is missing", async () => {
    const degraded: SystemHealth = {
      ...healthy, status: "degraded", schema: "not_ready",
      profile: "unknown", reason: "SCHEMA_NOT_READY",
    };

    expect(await getSystemHealth(fetchWithResponse(degraded, 503))).toMatchObject({
      status: "degraded",
      database: "connected",
      schema: "not_ready",
    });
  });

  it("does not expose driver errors or secrets", async () => {
    const secret = "super-secret-token";
    const fetchHealth = vi.fn(async () => {
      throw new Error(`failed for ${secret} at libsql://private.example`);
    }) as typeof fetch;

    const serialized = JSON.stringify(await getSystemHealth(fetchHealth));
    expect(serialized).not.toContain(secret);
    expect(serialized).not.toContain("libsql://");
  });
});
