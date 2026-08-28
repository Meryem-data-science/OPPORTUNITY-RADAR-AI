import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));

import { getSourceHealth, loadSourceHealth } from "./source-health";

const entry = {
  source_id: "test_source",
  enabled: true,
  last_run_at: "2026-08-03T09:00:00+00:00",
  status: "SUCCESS",
  items_found: 0,
  new_items: 0,
  relevant_items: null,
  error_type: null,
  error_message: null,
  zero_result_streak: 3,
  anomaly_code: "ZERO_RESULTS_STREAK",
  anomaly_message:
    "0 résultat trouvé lors de 3 exécutions réussies consécutives. Défaillance possible du collecteur ou du parseur.",
};
const fixture = { items: [entry], returned: 1 };

afterEach(() => {
  vi.unstubAllGlobals();
  delete process.env.OPPORTUNITY_API_BASE_URL;
});

describe("getSourceHealth", () => {
  it("fetches and maps a valid API response without caching it", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(fixture)));
    vi.stubGlobal("fetch", fetchMock);

    await expect(getSourceHealth()).resolves.toEqual(fixture);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8000/api/source-health",
      expect.objectContaining({ cache: "no-store" }),
    );
  });

  it("uses the configured API base URL without a trailing slash", async () => {
    process.env.OPPORTUNITY_API_BASE_URL = "https://api.example.test/";
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(fixture)));
    vi.stubGlobal("fetch", fetchMock);

    await getSourceHealth();

    expect(fetchMock).toHaveBeenCalledWith(
      "https://api.example.test/api/source-health",
      expect.objectContaining({ cache: "no-store" }),
    );
  });

  it("keeps every unknown value as null instead of coercing it", async () => {
    const neverRun = {
      items: [
        {
          ...entry,
          source_id: "never_run",
          last_run_at: null,
          status: null,
          items_found: null,
          new_items: null,
          relevant_items: null,
          zero_result_streak: 0,
          anomaly_code: null,
          anomaly_message: null,
        },
      ],
      returned: 1,
    };
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(JSON.stringify(neverRun))),
    );

    const response = await getSourceHealth();

    expect(response.items[0].items_found).toBeNull();
    expect(response.items[0].status).toBeNull();
    expect(response.items[0].last_run_at).toBeNull();
  });

  it("rejects a response whose entries lose the backend anomaly decision", async () => {
    const invalid = {
      items: [{ ...entry, anomaly_code: undefined }],
      returned: 1,
    };
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(JSON.stringify(invalid))),
    );

    await expect(getSourceHealth()).rejects.toThrow(
      "Source health API response is invalid",
    );
  });

  it("rejects a response whose streak is not a number", async () => {
    const invalid = { items: [{ ...entry, zero_result_streak: "3" }], returned: 1 };
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(JSON.stringify(invalid))),
    );

    await expect(getSourceHealth()).rejects.toThrow(
      "Source health API response is invalid",
    );
  });

  it("rejects an unsuccessful API response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response("", { status: 503 })),
    );

    await expect(getSourceHealth()).rejects.toThrow(
      "Source health API request failed",
    );
  });
});

describe("loadSourceHealth", () => {
  it("returns null instead of propagating a transport failure", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("ECONNREFUSED")));

    await expect(loadSourceHealth()).resolves.toBeNull();
  });

  it("returns the validated payload when the API answers", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(JSON.stringify(fixture))),
    );

    await expect(loadSourceHealth()).resolves.toEqual(fixture);
  });
});
