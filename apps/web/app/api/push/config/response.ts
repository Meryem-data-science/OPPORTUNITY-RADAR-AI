import { getPushConfig, type PushConfig } from "@/lib/push";

export async function createPushConfigResponse(
  load: () => Promise<PushConfig> = getPushConfig,
): Promise<Response> {
  const config = await load();
  return Response.json(config, {
    status: 200,
    headers: { "cache-control": "no-store" },
  });
}
