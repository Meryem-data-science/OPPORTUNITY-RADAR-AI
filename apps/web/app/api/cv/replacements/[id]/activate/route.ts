import { activateResponse } from "../../response";

export const dynamic = "force-dynamic";

/** Apply the reviewed replacement, against the token the person confirmed. */
export async function POST(
  request: Request,
  context: { params: Promise<{ id: string }> },
): Promise<Response> {
  const { id } = await context.params;
  return activateResponse(request, id);
}
