import { createApplicationResponse } from "./response";

export const dynamic = "force-dynamic";

export async function POST(request: Request): Promise<Response> {
  return createApplicationResponse(request);
}
