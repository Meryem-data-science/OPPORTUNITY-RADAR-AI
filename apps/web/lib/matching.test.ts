import { beforeEach, describe, expect, it, vi } from "vitest";
vi.mock("server-only", () => ({}));
import { getMatching, loadMatching } from "./matching";

const ready = {
  profile_id: 8, status: "READY", persistence_version: "p1", selection_version: "s1", history_count: 1,
  integrity: { ok: true, audit_version: "a1", audit_fingerprint: "audit" },
  current_run: {
    run_id: 4, created_at: "2026-01-01", assessment_count: 1,
    persistence_version: "p1", selection_version: "s1", matching_engine_version: "e1",
    matching_rules_version: "r1", semantic_percentile_version: "sp1", run_fingerprint: "run", batch_fingerprint: "batch",
    lane_counts: { PRIMARY: 1, UNCERTAIN: 0, OUTSIDE_PREFERENCES: 0 },
    items: [{ opportunity_id: 2, opportunity: { id: 2, canonical_title: "Role", organization: "Org", location: null, last_seen_at: "now", original_url: "https://example.invalid" }, matching: { lane: "PRIMARY", match_quality: null, evidence_coverage: .5, assessment_fingerprint: "item", explanation: { required_skill: { normalized_score: null } } } }],
  },
};

describe("matching data layer", () => {
  beforeEach(() => { vi.unstubAllGlobals(); vi.stubEnv("OPPORTUNITY_API_BASE_URL", "https://api.example.test/"); });
  it("fetches the exact endpoint without caching and accepts null quality", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ready });
    vi.stubGlobal("fetch", fetchMock);
    expect(await getMatching()).toEqual(ready);
    expect(fetchMock).toHaveBeenCalledWith("https://api.example.test/api/matching", expect.objectContaining({ cache: "no-store", signal: expect.any(AbortSignal) }));
  });
  it.each(["NOT_SYNCED", "EMPTY"])("accepts %s", async (status) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({ ...ready, status, current_run: null }) }));
    expect((await getMatching()).status).toBe(status);
  });
  it.each([
    { ...ready, status: "BROKEN" },
    { ...ready, current_run: { ...ready.current_run, lane_counts: { PRIMARY: 1, UNCERTAIN: 0 } } },
    { ...ready, current_run: { ...ready.current_run, items: [{ ...ready.current_run.items[0], matching: { ...ready.current_run.items[0].matching, lane: "BROKEN" } }] } },
    { ...ready, current_run: { ...ready.current_run, items: [{}] } },
  ])("rejects malformed payloads", async (payload) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => payload }));
    await expect(getMatching()).rejects.toThrow("invalid");
    expect(await loadMatching()).toBeNull();
  });
  it("throws on HTTP failure while the tolerant loader returns null", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false }));
    await expect(getMatching()).rejects.toThrow("failed");
    expect(await loadMatching()).toBeNull();
  });
});
