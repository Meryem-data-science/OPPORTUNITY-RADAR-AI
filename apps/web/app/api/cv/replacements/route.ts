import { currentReviewResponse, openReviewResponse } from "./response";

export const dynamic = "force-dynamic";

/** The open review of the configured profile, or the absence of one. */
export async function GET(): Promise<Response> {
  return currentReviewResponse();
}

/** Open a review over an extraction this profile already holds. */
export async function POST(request: Request): Promise<Response> {
  return openReviewResponse(request);
}
