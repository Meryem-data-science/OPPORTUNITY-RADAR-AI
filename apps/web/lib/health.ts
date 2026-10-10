import "server-only";

const DEFAULT_API_BASE_URL = "http://127.0.0.1:8000";

const HEALTH_REASONS = [
  "CONFIGURATION_INVALID", "DATABASE_UNAVAILABLE", "SCHEMA_NOT_READY",
  "SCHEMA_UNREADABLE", "BACKEND_NOT_OPERATIONAL", "PROFILE_NOT_CONFIGURED",
  "PROFILE_NOT_FOUND", "API_UNAVAILABLE", "API_RESPONSE_INVALID",
] as const;

export type HealthReason = (typeof HEALTH_REASONS)[number];

export type SystemHealth = {
  status: "ok" | "degraded" | "error";
  api: "available" | "unavailable";
  databaseBackend: "sqlite" | "turso" | "unknown";
  database: "connected" | "unavailable";
  schema: "ready" | "not_ready" | "unknown";
  migrationVersion: string | null;
  minimumMigrationVersion: string;
  profile: "ready" | "not_configured" | "not_found" | "unknown";
  reason: HealthReason | null;
};

export const HEALTH_MESSAGES: Record<HealthReason, string> = {
  CONFIGURATION_INVALID: "La configuration du service de données est invalide.",
  DATABASE_UNAVAILABLE: "La base configurée est inaccessible. Vérifiez sa présence et les droits de lecture.",
  SCHEMA_NOT_READY: "Le schéma produit est incomplet. Les migrations jusqu’à 0026 sont nécessaires.",
  SCHEMA_UNREADABLE: "La base répond, mais son schéma ne peut pas être lu correctement.",
  BACKEND_NOT_OPERATIONAL: "Le produit utilise SQLite. Le backend configuré ne prend pas en charge toutes les pages.",
  PROFILE_NOT_CONFIGURED: "Le profil serveur est absent ou invalide. Configurez OPPORTUNITY_RADAR_PROFILE_ID.",
  PROFILE_NOT_FOUND: "Le profil configuré n’existe pas dans cette base.",
  API_UNAVAILABLE: "Le service de données ne répond pas. Démarrez l’API et vérifiez sa configuration.",
  API_RESPONSE_INVALID: "Le service répond, mais son diagnostic est incompatible avec cette version de l’application.",
};

function publicHealth(payload: unknown): SystemHealth | null {
  if (typeof payload !== "object" || payload === null) return null;
  const value = payload as Record<string, unknown>;
  if (
    !["ok", "degraded", "error"].includes(String(value.status)) ||
    value.api !== "available" ||
    !["sqlite", "turso", "unknown"].includes(String(value.databaseBackend)) ||
    !["connected", "unavailable"].includes(String(value.database)) ||
    !["ready", "not_ready", "unknown"].includes(String(value.schema)) ||
    !["ready", "not_configured", "not_found", "unknown"].includes(String(value.profile)) ||
    !(value.migrationVersion === null ||
      (typeof value.migrationVersion === "string" && /^[0-9]{4,10}$/.test(value.migrationVersion))) ||
    !(typeof value.minimumMigrationVersion === "string" && /^[0-9]{4,10}$/.test(value.minimumMigrationVersion)) ||
    !(value.reason === null || HEALTH_REASONS.some((reason) => reason === value.reason))
  ) return null;
  if (value.status === "ok" && (
    value.database !== "connected" || value.schema !== "ready" ||
    value.profile !== "ready" || value.databaseBackend !== "sqlite" || value.reason !== null
  )) return null;

  // Copy only the agreed public fields, even if a backend sends extra data.
  return {
    status: value.status as SystemHealth["status"],
    api: "available",
    databaseBackend: value.databaseBackend as SystemHealth["databaseBackend"],
    database: value.database as SystemHealth["database"],
    schema: value.schema as SystemHealth["schema"],
    migrationVersion: value.migrationVersion as string | null,
    minimumMigrationVersion: value.minimumMigrationVersion,
    profile: value.profile as SystemHealth["profile"],
    reason: value.reason as HealthReason | null,
  };
}

function unavailableHealth(api: SystemHealth["api"], reason: HealthReason): SystemHealth {
  return {
    status: "error", api, databaseBackend: "unknown", database: "unavailable",
    schema: "unknown", migrationVersion: null, minimumMigrationVersion: "0026",
    profile: "unknown", reason,
  };
}

/** Read the same FastAPI service as the product; never connect to Turso here. */
export async function getSystemHealth(
  fetchHealth: typeof fetch = fetch,
): Promise<SystemHealth> {
  const baseUrl = (process.env.OPPORTUNITY_API_BASE_URL ?? DEFAULT_API_BASE_URL).replace(/\/$/, "");
  let response: Response;
  try {
    response = await fetchHealth(`${baseUrl}/api/health`, {
      cache: "no-store",
      signal: AbortSignal.timeout(5_000),
    });
  } catch {
    return unavailableHealth("unavailable", "API_UNAVAILABLE");
  }
  try {
    const health = publicHealth(await response.json());
    const expectedStatus = health?.status === "ok" ? 200 : 503;
    if (health !== null && response.status === expectedStatus) return health;
  } catch {
    // JSON and driver errors must never enter the public diagnostic.
  }
  return unavailableHealth("available", "API_RESPONSE_INVALID");
}
