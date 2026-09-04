import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));

import {
  forwardCreate,
  forwardStatus,
  forwardTracking,
  getApplications,
  loadApplication,
  loadApplications,
} from "./applications";
import {
  trackedApplication,
  trackedApplications,
  trackedDetail,
} from "./applications.fixture";

function respond(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("the applications loader", () => {
  it("reads the real API and keeps the real original_url", async () => {
    const fetchMock = vi.fn(async () => respond(trackedApplications));
    vi.stubGlobal("fetch", fetchMock);

    const response = await getApplications();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe("http://127.0.0.1:8000/api/applications");
    expect(response.items[0].opportunity.original_url).toBe(
      "https://careers.example.invalid/apply/42",
    );
  });

  it("honours the configured backend base URL", async () => {
    const fetchMock = vi.fn(async () => respond(trackedApplications));
    vi.stubGlobal("fetch", fetchMock);
    vi.stubEnv("OPPORTUNITY_API_BASE_URL", "http://api.example.invalid/");

    await getApplications();

    expect(fetchMock.mock.calls[0][0]).toBe(
      "http://api.example.invalid/api/applications",
    );
  });

  it("reports an unavailable surface as null rather than throwing", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => respond({ detail: "nope" }, 503)));
    await expect(loadApplications()).resolves.toBeNull();

    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new Error("connection refused");
      }),
    );
    await expect(loadApplications()).resolves.toBeNull();
    await expect(loadApplication(7)).resolves.toBeNull();
  });

  it("refuses a response whose shape it does not recognise", async () => {
    const invalid: unknown[] = [
      { profile_id: 1, items: [], total: 1 },
      { profile_id: 0, items: [], total: 0 },
      { profile_id: 1, items: [{ ...trackedApplication, status: "APPLIED" }], total: 1 },
      {
        profile_id: 1,
        items: [
          {
            ...trackedApplication,
            opportunity: { ...trackedApplication.opportunity, original_url: "" },
          },
        ],
        total: 1,
      },
      { profile_id: 1, items: [{ ...trackedApplication, opportunity_id: 99 }], total: 1 },
      "applications",
    ];
    for (const payload of invalid) {
      vi.stubGlobal("fetch", vi.fn(async () => respond(payload)));
      await expect(getApplications()).rejects.toThrow();
      await expect(loadApplications()).resolves.toBeNull();
    }
  });

  it("reads one candidature with its timeline", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => respond(trackedDetail)));

    const detail = await loadApplication(7);

    expect(detail?.events.map((event) => event.event_type)).toEqual([
      "APPLICATION_CREATED",
      "STATUS_CHANGED",
      "TRACKING_UPDATED",
    ]);
  });
});

describe("the applications write proxy", () => {
  it("sends every mutation to FastAPI and never touches a database", async () => {
    const result = { created: true, changed: true, application: trackedDetail };
    const fetchMock = vi.fn(async () => respond(result, 201));
    vi.stubGlobal("fetch", fetchMock);

    const created = await forwardCreate({ opportunity_id: 42, action: "SAVE" });

    expect(created).toEqual({ ok: true, status: 201, result });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://127.0.0.1:8000/api/applications");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({
      opportunity_id: 42,
      action: "SAVE",
    });

    await forwardStatus(7, { status: "INTERVIEW" });
    expect(fetchMock.mock.calls[1][0]).toBe(
      "http://127.0.0.1:8000/api/applications/7/status",
    );

    await forwardTracking(7, { notes: null });
    expect(fetchMock.mock.calls[2][0]).toBe("http://127.0.0.1:8000/api/applications/7");
    expect((fetchMock.mock.calls[2][1] as RequestInit).method).toBe("PATCH");
  });

  it("keeps the backend's class of failure without relaying its message", async () => {
    for (const status of [400, 404, 409, 503]) {
      vi.stubGlobal(
        "fetch",
        vi.fn(async () => respond({ detail: "internal detail" }, status)),
      );
      await expect(forwardCreate({})).resolves.toEqual({ ok: false, status });
    }
  });

  it("reports an unreachable or nonsensical backend without throwing", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new Error("connection refused");
      }),
    );
    await expect(forwardCreate({})).resolves.toEqual({ ok: false, status: 503 });

    vi.stubGlobal("fetch", vi.fn(async () => respond({ created: true })));
    await expect(forwardCreate({})).resolves.toEqual({ ok: false, status: 502 });
  });
});

describe("the browser boundary", () => {
  it("never opens a database and never publishes the backend URL", async () => {
    const { readFileSync } = await import("node:fs");
    const sources = [
      "./applications.ts",
      "./application-contract.ts",
      "../components/application-actions.tsx",
      "../components/application-tracking.tsx",
      "../app/applications/page.tsx",
      "../app/applications/[id]/page.tsx",
      "../app/api/applications/response.ts",
    ].map((path) => readFileSync(new URL(path, import.meta.url), "utf8"));

    // Checked as code rather than as prose: the prose in these files talks
    // about SQLite precisely because none of them may touch it.
    for (const source of sources) {
      const code = source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/[^\n]*/g, "");
      for (const forbidden of [
        "libsql",
        "@/lib/database",
        "process.env.NEXT_PUBLIC",
        "createClient",
      ]) {
        expect(code).not.toContain(forbidden);
      }
    }
  });
});
