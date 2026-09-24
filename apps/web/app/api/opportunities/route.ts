import { opportunitiesResponse } from "./response";

export const dynamic = "force-dynamic";

/** One page of the opportunity listing, for the browser. */
export async function GET(request: Request): Promise<Response> {
  return opportunitiesResponse(request);
}
