import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));

import { getOpportunities, loadOpportunities } from "./opportunities";

const fixture = {
  items: [
    {
      id: 42,
      canonical_title: "Test Platform Engineer",
      organization: "Fixture Labs",
      location: "Remote",
      original_url: "https://jobs.example.test/42",
      last_seen_at: "2026-08-09T10:00:00Z",
    },
  ],
  returned: 1,
  total: 1,
};

afterEach(() => {
  vi.unstubAllGlobals();
  delete process.env.OPPORTUNITY_API_BASE_URL;
});

describe("getOpportunities", () => {
  it("fetches and maps a valid API response without caching it", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(fixture)));
    vi.stubGlobal("fetch", fetchMock);

    await expect(getOpportunities()).resolves.toEqual(fixture);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/opportunities?limit=20",
      expect.objectContaining({ cache: "no-store" }),
    );
  });

  it("accepts a valid API opportunity with a null location", async () => {
    const responseWithNullLocation = {
      ...fixture,
      items: [{ ...fixture.items[0], location: null }],
    };
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(JSON.stringify(responseWithNullLocation))),
    );

    await expect(getOpportunities()).resolves.toEqual(responseWithNullLocation);
  });

  it("uses OPPORTUNITY_API_BASE_URL for the server API origin", async () => {
    process.env.OPPORTUNITY_API_BASE_URL = "http://fastapi.internal:9000/";
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(fixture)));
    vi.stubGlobal("fetch", fetchMock);

    await getOpportunities();

    expect(fetchMock).toHaveBeenCalledWith(
      "http://fastapi.internal:9000/api/opportunities?limit=20",
      expect.any(Object),
    );
  });

  it("rejects a non-successful HTTP response", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("unavailable", { status: 503 })));
    await expect(getOpportunities()).rejects.toThrow("Opportunity API request failed");
  });

  it("rejects a response missing fields required by the interface", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ items: [] }))));
    await expect(getOpportunities()).rejects.toThrow("Opportunity API response is invalid");
  });

  it("returns a safe null state when the API is unavailable", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("secret network details")));
    await expect(loadOpportunities()).resolves.toBeNull();
  });
});
