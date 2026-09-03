import { createPushConfigResponse } from "./response";

export const dynamic = "force-dynamic";

export async function GET(): Promise<Response> {
  return createPushConfigResponse();
}
