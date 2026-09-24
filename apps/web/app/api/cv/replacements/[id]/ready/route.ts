import { readyResponse } from "../../response";

export const dynamic = "force-dynamic";

/** Declare the review complete. Takes no body, and refuses one. */
export async function POST(
  request: Request,
  context: { params: Promise<{ id: string }> },
): Promise<Response> {
  const { id } = await context.params;
  return readyResponse(request, id);
}
