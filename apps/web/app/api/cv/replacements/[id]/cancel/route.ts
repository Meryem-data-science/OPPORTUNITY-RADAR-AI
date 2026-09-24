import { cancelResponse } from "../../response";

export const dynamic = "force-dynamic";

/** Abandon an open review. Takes no body, and refuses one. */
export async function POST(
  request: Request,
  context: { params: Promise<{ id: string }> },
): Promise<Response> {
  const { id } = await context.params;
  return cancelResponse(request, id);
}
