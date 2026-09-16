import { beforeEach, describe, expect, it, vi } from "vitest";
vi.mock("server-only", () => ({}));
import { explorerSearchParams, getExplorer, loadExplorer } from "./explorer";

const item = (id: number, overrides: Record<string, unknown> = {}) => ({
  opportunity_id: id,
  canonical_title: `Role ${id}`,
  organization: "Org",
  raw_location: "TEST ONLY place",
  original_url: `https://example.invalid/${id}?a=1&b=2`,
  last_seen_at: "2026-06-15T10:00:00+00:00",
  fine_primary_category: "DATA_ENGINEERING",
  opportunity_type: "INTERNSHIP",
  resolved_locations: [{ country_code: "XX", city_key: "test-city" }, { country_code: "YY", city_key: null }],
  has_unresolved_location: false,
  sources: [{ source_id: "board_a", source_type: "greenhouse" }],
  ...overrides,
});
const filters = {
  countries: ["XX", "YY"],
  cities: [{ country_code: "XX", city_key: "test-city" }],
  opportunity_types: ["INTERNSHIP", "JUNIOR_ROLE"],
  domains: ["DATA_ENGINEERING", "OTHER"],
  sources: [{ source_id: "board_a", source_type: "greenhouse" }],
};
const response = (items: unknown[], overrides: Record<string, unknown> = {}) => ({
  items, returned: items.length, total: 40, limit: 24, offset: 0, available_filters: filters, ...overrides,
});
const stub = (payload: unknown) => {
  const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => payload });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
};

describe("explorer data layer", () => {
  beforeEach(() => { vi.unstubAllGlobals(); vi.stubEnv("OPPORTUNITY_API_BASE_URL", "https://api.example.test/"); });

  it("fetches the exact endpoint without caching when no filter is active", async () => {
    const payload = response([item(1)]);
    const fetchMock = stub(payload);
    expect(await getExplorer()).toEqual(payload);
    expect(fetchMock).toHaveBeenCalledWith("https://api.example.test/api/explorer", expect.objectContaining({ cache: "no-store", signal: expect.any(AbortSignal) }));
  });

  it("emits every supported filter, in a stable order, and nothing else", async () => {
    const fetchMock = stub(response([]));
    await getExplorer({
      country: "XX", city: "test-city", opportunity_type: "INTERNSHIP", domain: "OTHER",
      source: "board_a", freshness: "7d", limit: 10, offset: 20,
    });
    const url = new URL(fetchMock.mock.calls[0][0]);
    expect(url.pathname).toBe("/api/explorer");
    expect([...url.searchParams.keys()]).toEqual(["country", "city", "opportunity_type", "domain", "source", "freshness", "limit", "offset"]);
    expect(Object.fromEntries(url.searchParams)).toEqual({
      country: "XX", city: "test-city", opportunity_type: "INTERNSHIP", domain: "OTHER",
      source: "board_a", freshness: "7d", limit: "10", offset: "20",
    });
  });

  it("emits only defined parameters and never a profile", async () => {
    const fetchMock = stub(response([]));
    await getExplorer({ domain: "DATA_SCIENCE", offset: 0, city: undefined });
    const called: string = fetchMock.mock.calls[0][0];
    expect(called).toBe("https://api.example.test/api/explorer?domain=DATA_SCIENCE&offset=0");
    expect(called).not.toContain("profile");
    expect([...explorerSearchParams({ country: undefined, source: "x" }).keys()]).toEqual(["source"]);
  });

  it("encodes values safely", async () => {
    const fetchMock = stub(response([]));
    await getExplorer({ city: "saint-étienne & co/?#", source: "a b=c" });
    const called: string = fetchMock.mock.calls[0][0];
    expect(called).not.toContain(" ");
    expect(called).not.toContain("#");
    const url = new URL(called);
    expect(url.searchParams.get("city")).toBe("saint-étienne & co/?#");
    expect(url.searchParams.get("source")).toBe("a b=c");
    expect([...url.searchParams.keys()]).toEqual(["city", "source"]);
  });

  it("returns a valid payload unchanged and preserves item order", async () => {
    const payload = response([item(9), item(2), item(5)]);
    stub(payload);
    const result = await getExplorer();
    expect(result).toEqual(payload);
    expect(result.items.map((entry) => entry.opportunity_id)).toEqual([9, 2, 5]);
  });

  it("accepts OTHER, a null category, a null type and a null raw location as themselves", async () => {
    stub(response([
      item(1, { fine_primary_category: "OTHER" }),
      item(2, { fine_primary_category: null, opportunity_type: null, raw_location: null, resolved_locations: [], has_unresolved_location: true, sources: [] }),
    ]));
    const [other, unknown] = (await getExplorer()).items;
    expect(other.fine_primary_category).toBe("OTHER");
    expect(unknown.fine_primary_category).toBeNull();
    expect(unknown.opportunity_type).toBeNull();
    expect(unknown.raw_location).toBeNull();
  });

  const base = item(1);
  it.each([
    ["a non-object payload", "nope"],
    ["missing items", { ...response([]), items: undefined }],
    ["returned disagreeing with items", response([base], { returned: 2 })],
    ["a negative total", response([base], { total: -1 })],
    ["a fractional limit", response([base], { limit: 2.5 })],
    ["a missing offset", response([base], { offset: undefined })],
    ["a string opportunity id", response([{ ...base, opportunity_id: "1" }])],
    ["a missing original URL", response([{ ...base, original_url: undefined }])],
    ["an unknown fine category", response([{ ...base, fine_primary_category: "ROBOTICS" }])],
    ["an unknown opportunity type", response([{ ...base, opportunity_type: "CDI" }])],
    ["an undefined category", response([{ ...base, fine_primary_category: undefined }])],
    ["a non-boolean unresolved flag", response([{ ...base, has_unresolved_location: "false" }])],
    ["a malformed resolved location", response([{ ...base, resolved_locations: [{ country_code: null, city_key: null }] }])],
    ["a malformed source", response([{ ...base, sources: [{ source_id: "a" }] }])],
    ["missing available filters", response([base], { available_filters: undefined })],
    ["a null city option", response([base], { available_filters: { ...filters, cities: [{ country_code: "XX", city_key: null }] } })],
    ["an unknown domain option", response([base], { available_filters: { ...filters, domains: ["UNKNOWN"] } })],
    ["an unknown type option", response([base], { available_filters: { ...filters, opportunity_types: ["NO_TYPE"] } })],
    ["a non-string country option", response([base], { available_filters: { ...filters, countries: [null] } })],
    ["a malformed source option", response([base], { available_filters: { ...filters, sources: [{ source_type: "x" }] } })],
  ])("rejects %s", async (_label, payload) => {
    stub(payload);
    await expect(getExplorer()).rejects.toThrow("invalid");
    expect(await loadExplorer()).toBeNull();
  });

  it("throws a generic error on a non-2xx answer while the tolerant loader returns null", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 503, json: async () => ({ detail: "secret-path" }) }));
    await expect(getExplorer()).rejects.toThrow("Explorer API request failed");
    expect(await loadExplorer()).toBeNull();
  });

  it("returns null when the request itself fails", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("ECONNREFUSED")));
    expect(await loadExplorer({ domain: "OTHER" })).toBeNull();
  });
});
