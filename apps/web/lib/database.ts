import "server-only";

import { createClient, type Client } from "@libsql/client";

export type DatabaseClient = Pick<Client, "execute">;

export function createDatabaseClient(): DatabaseClient {
  const url = process.env.TURSO_DATABASE_URL;
  const authToken = process.env.TURSO_AUTH_TOKEN;

  if (!url || !authToken) {
    throw new Error("Turso database configuration is unavailable");
  }

  return createClient({ url, authToken });
}
