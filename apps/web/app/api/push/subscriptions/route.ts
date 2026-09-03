import {
  createSubscribeResponse,
  createUnsubscribeResponse,
} from "./response";

export const dynamic = "force-dynamic";

export async function POST(request: Request): Promise<Response> {
  return createSubscribeResponse(request);
}

export async function DELETE(request: Request): Promise<Response> {
  return createUnsubscribeResponse(request);
}
