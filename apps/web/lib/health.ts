import "server-only";

import { createDatabaseClient, type DatabaseClient } from "@/lib/database";

const REQUIRED_TABLES = new Set([
  "schema_migrations",
  "sources",
  "opportunities",
  "opportunity_sources",
]);
const EXPECTED_MIGRATION_VERSION = "0001";

export type SystemHealth =
  | {
      status: "ok";
      database: "connected";
      schema: "ready";
      migrationVersion: string;
    }
  | {
      status: "error";
      database: "connected" | "unavailable";
      schema: "not_ready" | "unknown";
      migrationVersion: string | null;
    };

export async function getSystemHealth(
  createClient: () => DatabaseClient = createDatabaseClient,
): Promise<SystemHealth> {
  let database: DatabaseClient;

  try {
    database = createClient();
    const connectivity = await database.execute("SELECT 1 AS value");
    if (connectivity.rows[0]?.value !== 1) {
      throw new Error("Unexpected connectivity result");
    }
  } catch {
    return {
      status: "error",
      database: "unavailable",
      schema: "unknown",
      migrationVersion: null,
    };
  }

  try {
    const schema = await database.execute(
      "SELECT name FROM sqlite_schema WHERE type = 'table' ORDER BY name",
    );
    const tableNames = new Set(schema.rows.map((row) => String(row.name)));
    const hasFoundationSchema = [...REQUIRED_TABLES].every((table) =>
      tableNames.has(table),
    );

    if (!hasFoundationSchema) {
      return {
        status: "error",
        database: "connected",
        schema: "not_ready",
        migrationVersion: null,
      };
    }

    const migrations = await database.execute(
      "SELECT version FROM schema_migrations ORDER BY version",
    );
    const migrationVersion = migrations.rows.at(-1)?.version;

    if (migrationVersion !== EXPECTED_MIGRATION_VERSION) {
      return {
        status: "error",
        database: "connected",
        schema: "not_ready",
        migrationVersion:
          typeof migrationVersion === "string" ? migrationVersion : null,
      };
    }

    return {
      status: "ok",
      database: "connected",
      schema: "ready",
      migrationVersion,
    };
  } catch {
    return {
      status: "error",
      database: "connected",
      schema: "not_ready",
      migrationVersion: null,
    };
  }
}
