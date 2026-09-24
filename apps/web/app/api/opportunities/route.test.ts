import { describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));

import {
  INVALID_REQUEST,
  UNAVAILABLE,
  opportunitiesResponse,
  pageNumber,
} from "./response";
import {
  OPPORTUNITIES_MAX_LIMIT,
  OPPORTUNITIES_PAGE_SIZE,
  type OpportunitiesResponse,
} from "@/lib/opportunity-contract";

function offer(id: number) {
  return {
    id,
    canonical_title: `TEST ONLY role ${id}`,
    organization: "TEST ONLY organization",
    location: null,
    original_url: `https://example.invalid/${id}`,
    last_seen_at: "2026-01-01T00:00:00+00:00",
  };
}

function answer(ids: number[], total: number): OpportunitiesResponse {
  return { items: ids.map(offer), returned: ids.length, total };
}

const request = (query: string) =>
  new Request(`https://app.example.invalid/api/opportunities${query}`);

describe("reading the page parameters", () => {
  it("falls back when a parameter is absent", () => {
    expect(pageNumber(null, { fallback: 20, min: 1, max: 100 })).toBe(20);
    expect(pageNumber(null, { fallback: 0, min: 0, max: 99 })).toBe(0);
  });

  it("accepts a well-formed value inside its bounds", () => {
    expect(pageNumber("1", { fallback: 20, min: 1, max: 100 })).toBe(1);
    expect(pageNumber("100", { fallback: 20, min: 1, max: 100 })).toBe(100);
    expect(pageNumber("0", { fallback: 5, min: 0, max: 100 })).toBe(0);
  });

  it.each(["-1", "1.5", "abc", "", " 5", "05", "1e3", "٣"])(
    "refuses %o rather than quietly falling back",
    (raw) => {
      // A caller that asked for page 3 and silently received page 1 would show
      // the wrong list and never know.
      expect(pageNumber(raw, { fallback: 20, min: 1, max: 100 })).toBeNull();
    },
  );

  it("refuses a value outside its bounds", () => {
    expect(pageNumber("101", { fallback: 20, min: 1, max: 100 })).toBeNull();
    expect(pageNumber("0", { fallback: 20, min: 1, max: 100 })).toBeNull();
  });
});

describe("GET /api/opportunities", () => {
  it("asks for the default page when nothing is specified", async () => {
    const load = vi.fn(async () => answer([1, 2], 2));

    const response = await opportunitiesResponse(request(""), load);

    expect(load).toHaveBeenCalledWith(OPPORTUNITIES_PAGE_SIZE, 0);
    expect(response.status).toBe(200);
    expect(response.headers.get("cache-control")).toBe("no-store");
    await expect(response.json()).resolves.toEqual(answer([1, 2], 2));
  });

  it("forwards the requested page", async () => {
    const load = vi.fn(async () => answer([21], 21));

    await opportunitiesResponse(request("?limit=20&offset=20"), load);

    expect(load).toHaveBeenCalledWith(20, 20);
  });

  it("allows an offset far past the end, and returns what that is", async () => {
    // The listing grows; refusing to look past an arbitrary point would be a
    // limit this layer invented.
    const load = vi.fn(async () => answer([], 3));

    const response = await opportunitiesResponse(request("?offset=99999"), load);

    expect(load).toHaveBeenCalledWith(OPPORTUNITIES_PAGE_SIZE, 99999);
    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toMatchObject({ items: [], total: 3 });
  });

  it("keeps the ceiling of one hundred per request", async () => {
    const load = vi.fn(async () => answer([1], 1));

    const ok = await opportunitiesResponse(
      request(`?limit=${OPPORTUNITIES_MAX_LIMIT}`),
      load,
    );
    const refused = await opportunitiesResponse(
      request(`?limit=${OPPORTUNITIES_MAX_LIMIT + 1}`),
      load,
    );

    expect(ok.status).toBe(200);
    expect(refused.status).toBe(400);
    expect(load).toHaveBeenCalledTimes(1); // the refused one never reached it
    await expect(refused.json()).resolves.toEqual({ error: INVALID_REQUEST });
  });

  it.each(["?limit=abc", "?offset=-1", "?limit=0", "?offset=1.5"])(
    "refuses %s without asking the backend",
    async (query) => {
      const load = vi.fn();

      const response = await opportunitiesResponse(request(query), load);

      expect(load).not.toHaveBeenCalled();
      expect(response.status).toBe(400);
      expect(response.headers.get("cache-control")).toBe("no-store");
    },
  );

  it("reports an unreadable listing as unavailable, with a fixed sentence", async () => {
    const load = vi.fn(async () => null);

    const response = await opportunitiesResponse(request(""), load);

    expect(response.status).toBe(503);
    await expect(response.json()).resolves.toEqual({ error: UNAVAILABLE });
  });

  it("ignores parameters it does not know", async () => {
    const load = vi.fn(async () => answer([1], 1));

    const response = await opportunitiesResponse(
      request("?limit=5&offset=5&sort=whatever"),
      load,
    );

    expect(load).toHaveBeenCalledWith(5, 5);
    expect(response.status).toBe(200);
  });
});
