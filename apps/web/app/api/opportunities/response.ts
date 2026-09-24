import {
  OPPORTUNITIES_MAX_LIMIT,
  OPPORTUNITIES_PAGE_SIZE,
  loadOpportunities,
  type OpportunitiesResponse,
} from "@/lib/opportunities";

/**
 * The browser's only door to the opportunity listing.
 *
 * The page renders its first page on the server. Asking for the next one
 * happens in the browser, which cannot reach FastAPI directly — the backend
 * base URL is a server value and stays one — so it comes through this handler
 * on the app's own origin instead.
 *
 * It adds no rules beyond reading the two page parameters. Which opportunities
 * are visible, and how they are qualified, is decided by the backend and is not
 * touched here.
 */
export const INVALID_REQUEST = "Requête de page invalide.";
export const UNAVAILABLE = "Les opportunités sont temporairement indisponibles.";

const NO_STORE = { "cache-control": "no-store" } as const;

/**
 * Read one bounded, non-negative integer from the query string.
 *
 * An absent parameter falls back to the default. A present but malformed one
 * is an error rather than a silent fallback: a caller that asked for page 3
 * and receives page 1 without being told would show the wrong list and never
 * know.
 */
export function pageNumber(
  raw: string | null,
  { fallback, min, max }: { fallback: number; min: number; max: number },
): number | null {
  if (raw === null) return fallback;
  if (!/^(0|[1-9][0-9]{0,8})$/.test(raw)) return null;
  const value = Number(raw);
  return value < min || value > max ? null : value;
}

export async function opportunitiesResponse(
  request: Request,
  load: (
    limit: number,
    offset: number,
  ) => Promise<OpportunitiesResponse | null> = loadOpportunities,
): Promise<Response> {
  const parameters = new URL(request.url).searchParams;
  const limit = pageNumber(parameters.get("limit"), {
    fallback: OPPORTUNITIES_PAGE_SIZE,
    min: 1,
    max: OPPORTUNITIES_MAX_LIMIT,
  });
  // No upper bound on the offset: the listing grows, and refusing to look past
  // an arbitrary point would be this layer inventing a limit the backend does
  // not have. An offset past the end simply returns no items.
  const offset = pageNumber(parameters.get("offset"), {
    fallback: 0,
    min: 0,
    max: Number.MAX_SAFE_INTEGER,
  });
  if (limit === null || offset === null) {
    return Response.json(
      { error: INVALID_REQUEST },
      { status: 400, headers: NO_STORE },
    );
  }

  const page = await load(limit, offset);
  if (page === null) {
    return Response.json(
      { error: UNAVAILABLE },
      { status: 503, headers: NO_STORE },
    );
  }
  return Response.json(page, { headers: NO_STORE });
}
