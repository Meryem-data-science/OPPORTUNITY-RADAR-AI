/**
 * The shape of one collected opportunity, shared by both sides of the app.
 *
 * Deliberately free of `server-only`: the type and the page size are as true in
 * a browser control as in a server loader, and a client component that had to
 * reach into a server module to learn them would drag the backend base URL into
 * the bundle with it. Nothing here talks to anything — the fetching lives in
 * `lib/opportunities.ts` and in the route handler beside it.
 */

export type Opportunity = {
  id: number;
  canonical_title: string;
  organization: string;
  location: string | null;
  original_url: string;
  last_seen_at: string;
};

export type OpportunitiesResponse = {
  items: Opportunity[];
  returned: number;
  total: number;
};

/**
 * How many opportunities one page holds.
 *
 * The backend caps a single request at 100, so this is a page size and not a
 * ceiling on what can be read: the whole listing is reachable by asking for the
 * next page. 20 keeps the first paint small.
 */
export const OPPORTUNITIES_PAGE_SIZE = 20;

/** The backend's own ceiling, repeated so a caller cannot ask past it. */
export const OPPORTUNITIES_MAX_LIMIT = 100;

export function isOpportunity(value: unknown): value is Opportunity {
  if (typeof value !== "object" || value === null) return false;

  const opportunity = value as Record<string, unknown>;
  return (
    typeof opportunity.id === "number" &&
    typeof opportunity.canonical_title === "string" &&
    typeof opportunity.organization === "string" &&
    (typeof opportunity.location === "string" || opportunity.location === null) &&
    typeof opportunity.original_url === "string" &&
    typeof opportunity.last_seen_at === "string"
  );
}

export function isOpportunitiesResponse(
  value: unknown,
): value is OpportunitiesResponse {
  if (typeof value !== "object" || value === null) return false;

  const response = value as Record<string, unknown>;
  return (
    Array.isArray(response.items) &&
    response.items.every(isOpportunity) &&
    typeof response.returned === "number" &&
    typeof response.total === "number"
  );
}

/**
 * Where the walk stands: what is on screen, and what has been received.
 *
 * The two are not the same number, and conflating them is a real bug. A page
 * may contain an opportunity already displayed — the stable ordering is meant
 * to prevent it, but a collection landing mid-walk still can — and that row is
 * shown once. If the next request then started from the number of *cards*, it
 * would ask again for rows the API had already handed over, receive the same
 * page, and the button would never reach the end of the listing.
 *
 * So `items` counts what the person sees and `nextOffset` counts what the API
 * has produced. Only the second one drives the next request.
 */
export type OpportunityPageState = {
  items: readonly Opportunity[];
  /** Rows received from the API so far, duplicates included. */
  nextOffset: number;
  /** True once a page came back empty: there is nothing further to ask for. */
  exhausted: boolean;
};

/**
 * What one answer did to the walk.
 *
 * A malformed payload is its own outcome rather than an empty page. Treating
 * unreadable JSON as "no more offers" would end the listing early and call it
 * complete, which is the one wrong answer that looks right.
 */
export type OpportunityPageOutcome =
  | { kind: "appended"; state: OpportunityPageState }
  | { kind: "malformed" };

export function initialPageState(
  items: readonly Opportunity[],
): OpportunityPageState {
  return { items, nextOffset: items.length, exhausted: false };
}

export function applyOpportunityPage(
  state: OpportunityPageState,
  payload: unknown,
): OpportunityPageOutcome {
  if (!isOpportunitiesResponse(payload)) return { kind: "malformed" };
  const received = payload.items;
  const seen = new Set(state.items.map((item) => item.id));
  return {
    kind: "appended",
    state: {
      items: [...state.items, ...received.filter((item) => !seen.has(item.id))],
      // Advanced by the rows received, not by the cards added.
      nextOffset: state.nextOffset + received.length,
      // An empty page is the end, whatever a `total` read earlier now says.
      exhausted: received.length === 0,
    },
  };
}

/** Whether another page is worth asking for. */
export function hasMoreOpportunities(
  state: OpportunityPageState,
  total: number,
): boolean {
  return !state.exhausted && state.nextOffset < total;
}
