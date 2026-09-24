import { decisionResponse } from "../../response";

export const dynamic = "force-dynamic";

/** Record or replace one human answer about one plan entry. */
export async function PUT(
  request: Request,
  context: { params: Promise<{ id: string }> },
): Promise<Response> {
  const { id } = await context.params;
  return decisionResponse(request, id);
}
