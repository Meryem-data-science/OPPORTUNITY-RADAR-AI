import { updateStatusResponse } from "../../response";

export const dynamic = "force-dynamic";

export async function PATCH(
  request: Request,
  context: { params: Promise<{ id: string }> },
): Promise<Response> {
  const { id } = await context.params;
  return updateStatusResponse(request, id);
}
