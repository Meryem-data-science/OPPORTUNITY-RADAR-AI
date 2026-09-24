import React from "react";
import { readFileSync } from "node:fs";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: () => {} }) }));

import OpportunityList, { type TrackedStatuses } from "./opportunity-list";
import {
  applyOpportunityPage,
  hasMoreOpportunities,
  initialPageState,
  type Opportunity,
} from "@/lib/opportunity-contract";

// Normalized: this checkout uses CRLF, and a source assertion must be about
// the code rather than about which line ending Git happened to write.
const source = readFileSync(
  new URL("./opportunity-list.tsx", import.meta.url),
  "utf8",
).split("\r\n").join("\n");

function offer(id: number): Opportunity {
  return {
    id,
    canonical_title: `TEST ONLY role ${id}`,
    organization: `TEST ONLY organization ${id}`,
    location: id % 2 === 0 ? null : `TEST ONLY city ${id}`,
    original_url: `https://example.invalid/${id}`,
    last_seen_at: "2026-01-01T00:00:00+00:00",
  };
}

const render = (
  items: Opportunity[],
  total: number,
  tracked: TrackedStatuses = {},
) =>
  renderToStaticMarkup(
    <OpportunityList initial={items} total={total} tracked={tracked} />,
  );

describe("what the list shows", () => {
  it("renders every opportunity it was given", () => {
    const html = render([offer(1), offer(2)], 40);

    expect(html).toContain("TEST ONLY role 1");
    expect(html).toContain("TEST ONLY role 2");
    expect(html).toContain("TEST ONLY organization 1");
    expect(html).toContain("https://example.invalid/2");
  });

  it("says what a missing location is rather than leaving a gap", () => {
    expect(render([offer(2)], 1)).toContain("Lieu non précisé");
  });

  it("states how many of the total are on screen", () => {
    expect(render([offer(1), offer(2)], 40)).toContain(
      "2 opportunités affichées sur 40",
    );
  });

  it("offers the next page while there is one", () => {
    const html = render([offer(1)], 40);

    expect(html).toContain("Afficher plus");
    expect(html).not.toContain("Fin de la liste");
  });

  it("states the end of the list instead of a button that does nothing", () => {
    const html = render([offer(1), offer(2)], 2);

    expect(html).toContain("Fin de la liste");
    expect(html).toContain("Toutes les opportunités enregistrées sont");
    expect(html).not.toContain("Afficher plus");
  });

  it("treats a listing longer than the count as complete rather than looping", () => {
    // Defensive: a total that has shrunk since the first page must not leave a
    // button that can only ever return nothing.
    expect(render([offer(1), offer(2), offer(3)], 2)).toContain("Fin de la liste");
  });

  it("says plainly when there is nothing at all", () => {
    const html = render([], 0);

    expect(html).toContain("Aucune opportunité disponible");
    expect(html).not.toContain("Afficher plus");
  });

  it("passes the tracked status of each opportunity through", () => {
    const html = render([offer(1)], 1, { "1": "INTERVIEW" });
    expect(html).toContain("Entretien");
  });
});

describe("the component's own contract", () => {
  it("asks this app's own route and never the backend directly", () => {
    expect(source).toContain('fetch(\n        `/api/opportunities?limit=');
    for (const forbidden of [
      "127.0.0.1",
      "process.env",
      "server-only",
      "OPPORTUNITY_API_BASE_URL",
      "libsql",
      "createClient",
      "@/lib/opportunities",
    ]) {
      expect(source).not.toContain(forbidden);
    }
  });

  it("guards against a double click and never assumes a success", () => {
    expect(source).toContain("if (busy) return;");
    expect(source).toContain("disabled={busy}");
    expect(source).toContain("if (!response.ok)");
    // The state is set from the answer, not from the click.
    expect(source).toContain("await response.json()");
  });

  it("shows a loading state and an error, and retries only on request", () => {
    expect(source).toContain("Chargement…");
    expect(source).toContain("LOAD_FAILED");
    expect(source).toContain('role="alert"');
    // No automatic retry loop.
    expect(source).not.toContain("setTimeout");
    expect(source).not.toContain("setInterval");
  });

  it("claims nothing about an offer still being open", () => {
    for (const forbidden of ["encore ouvert", "toujours ouvert", "disponible à la candidature"]) {
      expect(source).not.toContain(forbidden);
    }
  });
});


describe("walking the listing across several answers", () => {
  /**
   * These are behavioural tests with real inputs and real outputs: each step
   * feeds `applyOpportunityPage` an answer shaped exactly as the route handler
   * returns one, and asserts the state it produces.
   *
   * LIMITATION, stated rather than papered over: they do not press the button.
   * This repository tests with vitest in `environment: "node"`, without jsdom
   * and without @testing-library/react, so a real click cannot be simulated
   * here without installing a dependency. The bookkeeping was extracted into
   * the contract module precisely so the part that can go wrong is tested for
   * real; the wiring of the button to it is asserted separately, and is
   * labelled as source inspection.
   */
  const answer = (ids: number[], total: number) => ({
    items: ids.map(offer),
    returned: ids.length,
    total,
  });

  it("advances by rows received, not by cards displayed", () => {
    // Step 1: the first page, three rows, three cards.
    let state = initialPageState([offer(1), offer(2), offer(3)]);
    expect(state.nextOffset).toBe(3);
    expect(state.exhausted).toBe(false);

    // Step 2: a page whose middle row is already on screen. Three rows are
    // received, only two are new.
    let outcome = applyOpportunityPage(state, answer([3, 4, 5], 8));
    expect(outcome.kind).toBe("appended");
    state = (outcome as { state: typeof state }).state;
    expect(state.items.map((item) => item.id)).toEqual([1, 2, 3, 4, 5]);
    expect(state.items).toHaveLength(5); // no duplicate displayed
    // The offset follows the six rows the API produced, not the five cards.
    expect(state.nextOffset).toBe(6);
    expect(hasMoreOpportunities(state, 8)).toBe(true);

    // Step 3: the page after the duplicate. Asking from 6 is what makes it the
    // right page; asking from 5 would have returned row 6 again.
    outcome = applyOpportunityPage(state, answer([6, 7], 8));
    state = (outcome as { state: typeof state }).state;
    expect(state.items.map((item) => item.id)).toEqual([1, 2, 3, 4, 5, 6, 7]);
    expect(state.nextOffset).toBe(8);
    expect(state.exhausted).toBe(false);

    // Step 4: the empty page. The walk ends.
    outcome = applyOpportunityPage(state, answer([], 8));
    state = (outcome as { state: typeof state }).state;
    expect(state.items).toHaveLength(7);
    expect(state.nextOffset).toBe(8);
    expect(state.exhausted).toBe(true);
    expect(hasMoreOpportunities(state, 8)).toBe(false);
  });

  it("stops offering more once a page comes back empty, stale total or not", () => {
    const state = initialPageState([offer(1)]);
    const outcome = applyOpportunityPage(state, answer([], 999));

    expect(outcome.kind).toBe("appended");
    const next = (outcome as { state: typeof state }).state;
    // The total says there should be 998 more. The API says there are none, and
    // the API is the one that just looked.
    expect(next.exhausted).toBe(true);
    expect(hasMoreOpportunities(next, 999)).toBe(false);
  });

  it("never asks the same offset twice in a row", () => {
    let state = initialPageState([offer(1), offer(2)]);
    const asked: number[] = [state.nextOffset];
    for (const ids of [[2, 3], [4, 5], [6]]) {
      const outcome = applyOpportunityPage(state, answer(ids, 10));
      state = (outcome as { state: typeof state }).state;
      asked.push(state.nextOffset);
    }
    expect(asked).toEqual([2, 4, 6, 7]);
    expect(new Set(asked).size).toBe(asked.length);
  });

  it("treats a malformed answer as a failure, never as the end", () => {
    const state = initialPageState([offer(1)]);

    for (const payload of [
      null,
      undefined,
      "not json",
      {},
      { items: "nope", returned: 0, total: 1 },
      { items: [{ id: 1 }], returned: 1, total: 1 },
      { items: [], returned: 0 },
      [offer(2)],
    ]) {
      const outcome = applyOpportunityPage(state, payload);
      expect(outcome.kind).toBe("malformed");
    }
    // The state is untouched, so the walk can be retried from where it was.
    expect(state.nextOffset).toBe(1);
    expect(state.exhausted).toBe(false);
    expect(hasMoreOpportunities(state, 5)).toBe(true);
  });

  it("ends the walk when the rows received reach the total", () => {
    const state = initialPageState([offer(1), offer(2)]);
    expect(hasMoreOpportunities(state, 2)).toBe(false);
    expect(hasMoreOpportunities(state, 3)).toBe(true);
  });

  it("keeps the displayed order of the first answer", () => {
    let state = initialPageState([offer(5), offer(3)]);
    const outcome = applyOpportunityPage(state, answer([1, 5], 4));
    state = (outcome as { state: typeof state }).state;
    // Appended in the order received, with the already-shown row skipped.
    expect(state.items.map((item) => item.id)).toEqual([5, 3, 1]);
  });
});

describe("the button is wired to that bookkeeping", () => {
  // NOTE: source inspection, not a simulated click — see the limitation above.
  it("asks from the rows received", () => {
    expect(source).toContain("offset=${page.nextOffset}");
    expect(source).not.toContain("offset=${loaded}");
    expect(source).not.toContain("offset=${items.length}");
  });

  it("routes every answer through the shared transition", () => {
    expect(source).toContain("applyOpportunityPage(page, await response.json())");
    expect(source).toContain('if (outcome.kind === "malformed")');
    expect(source).toContain("setPage(outcome.state)");
  });

  it("decides whether to offer more through the shared rule", () => {
    expect(source).toContain("hasMoreOpportunities(page, total)");
  });
});
