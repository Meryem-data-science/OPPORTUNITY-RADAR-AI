import { getSystemHealth, type SystemHealth } from "@/lib/health";

export async function createHealthResponse(
  checkHealth: () => Promise<SystemHealth> = getSystemHealth,
): Promise<Response> {
  const health = await checkHealth();

  return Response.json(health, {
    status: health.status === "ok" ? 200 : 503,
  });
}
