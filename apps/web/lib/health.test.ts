import { describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));

import { getSystemHealth } from "./health";
import type { DatabaseClient } from "./database";

const tables = [
  "schema_migrations",
  "sources",
  "opportunities",
  "opportunity_sources",
];

function databaseWithResponses(responses: Array<{ rows: Array<Record<string, string | number>> }>) {
  let index = 0;
  return {
    execute: async () => responses[index++],
  } as unknown as DatabaseClient;
}

describe("getSystemHealth", () => {
  it("reports a connected database with the complete foundation schema", async () => {
    const database = databaseWithResponses([
      { rows: [{ value: 1 }] },
      { rows: tables.map((name) => ({ name })) },
      { rows: [{ version: "0001" }] },
    ]);

    await expect(getSystemHealth(() => database)).resolves.toEqual({
      status: "ok",
      database: "connected",
      schema: "ready",
      migrationVersion: "0001",
    });
  });

  it("reports an unavailable database when SELECT 1 fails", async () => {
    const database = {
      execute: async () => {
        throw new Error("connection failed");
      },
    } as unknown as DatabaseClient;

    expect(await getSystemHealth(() => database)).toMatchObject({
      status: "error",
      database: "unavailable",
    });
  });

  it("reports a schema that is not ready when a table is missing", async () => {
    const database = databaseWithResponses([
      { rows: [{ value: 1 }] },
      { rows: tables.slice(0, -1).map((name) => ({ name })) },
    ]);

    expect(await getSystemHealth(() => database)).toMatchObject({
      status: "error",
      database: "connected",
      schema: "not_ready",
    });
  });

  it("does not expose driver errors or secrets", async () => {
    const secret = "super-secret-token";
    const database = {
      execute: async () => {
        throw new Error(`failed for ${secret} at libsql://private.example`);
      },
    } as unknown as DatabaseClient;

    const serialized = JSON.stringify(await getSystemHealth(() => database));
    expect(serialized).not.toContain(secret);
    expect(serialized).not.toContain("libsql://");
  });
});
