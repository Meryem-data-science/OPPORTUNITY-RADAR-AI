import { beforeEach, describe, expect, it, vi } from "vitest";
vi.mock("server-only", () => ({}));
import { getRecommendation, loadRecommendation } from "./recommendation";

const item = (id: number, rank: number, score: number | null, fine: string | null = "DATA_SCIENCE") => ({
  opportunity_id: id, rank_position: rank,
  opportunity: {
    id, canonical_title: `Role ${id}`, organization: "Org", location: null, last_seen_at: "now",
    original_url: `https://example.invalid/${id}`,
    fine_primary_category: fine,
    fine_secondary_categories: fine === null ? null : [],
    fine_category_evidence: fine === null ? null : [{ category: "DATA_SCIENCE", field: "TITLE", kind: "ROLE_PHRASE", signal: "data scientist" }],
    fine_reasons: fine === null ? null : ["reason"],
    fine_classifier_version: fine === null ? null : "fine-v1",
  },
  recommendation: {
    disposition: "RECOMMENDED", recommendation_score: score, evidence_coverage: 0.5, assessment_fingerprint: `a-${id}`,
    strengths: ["GEOGRAPHY_MATCHED"], confirmed_gaps: [], unknowns: ["ELIGIBILITY_UNKNOWN"],
    explanation: { result: { strengths: ["GEOGRAPHY_MATCHED"] }, anything: { nested: [1, null, "x"] } },
  },
});
const ready = {
  profile_id: 8, status: "READY", persistence_version: "p1", input_assembly_version: "i1", history_count: 2,
  readiness_issues: [], integrity: { ok: true, audit_version: "a1", audit_fingerprint: "audit" },
  current_run: {
    run_id: 4, created_at: "2026-01-01", assessment_count: 2, persistence_version: "p1", input_assembly_version: "i1",
    recommendation_engine_version: "e1", recommendation_rules_version: "r1", source_matching_run_id: 3,
    source_matching_run_fingerprint: "m", batch_fingerprint: "batch", run_fingerprint: "run",
    items: [item(2, 1, 0.4), item(5, 2, 0.95)],
  },
};
const withItems = (items: unknown[]) => ({ ...ready, current_run: { ...ready.current_run, assessment_count: items.length, items } });
const stub = (payload: unknown) => vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => payload }));

describe("recommendation data layer", () => {
  beforeEach(() => { vi.unstubAllGlobals(); vi.stubEnv("OPPORTUNITY_API_BASE_URL", "https://api.example.test/"); });

  it("fetches the exact endpoint without caching or a profile id and keeps the received order", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ready });
    vi.stubGlobal("fetch", fetchMock);
    const result = await getRecommendation();
    expect(result).toEqual(ready);
    expect(result.current_run!.items.map((entry) => entry.opportunity_id)).toEqual([2, 5]);
    expect(fetchMock).toHaveBeenCalledWith("https://api.example.test/api/recommendation", expect.objectContaining({ cache: "no-store", signal: expect.any(AbortSignal) }));
    expect(fetchMock.mock.calls[0][0]).not.toContain("profile");
  });

  it("accepts NOT_SYNCED without a run", async () => {
    stub({ ...ready, status: "NOT_SYNCED", persistence_version: null, input_assembly_version: null, history_count: 0, current_run: null });
    expect((await getRecommendation()).status).toBe("NOT_SYNCED");
  });

  it("accepts INCOMPLETE with readiness issues and no run", async () => {
    stub({ ...ready, status: "INCOMPLETE", readiness_issues: [{ code: "MATCHING_NOT_READY", opportunity_id: null }, { code: "OPPORTUNITY_MISSING", opportunity_id: 9 }], current_run: null });
    const result = await getRecommendation();
    expect(result.status).toBe("INCOMPLETE");
    expect(result.readiness_issues).toHaveLength(2);
  });

  it("throws on HTTP failure while the tolerant loader returns null", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false }));
    await expect(getRecommendation()).rejects.toThrow("failed");
    expect(await loadRecommendation()).toBeNull();
  });

  it("returns null from the tolerant loader when the network fails", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("ECONNREFUSED")));
    expect(await loadRecommendation()).toBeNull();
  });

  it("accepts a null score, which stays null", async () => {
    stub(withItems([item(2, 1, null)]));
    expect((await getRecommendation()).current_run!.items[0].recommendation.recommendation_score).toBeNull();
  });

  it("accepts a real zero score, which stays zero", async () => {
    stub(withItems([item(2, 1, 0)]));
    expect((await getRecommendation()).current_run!.items[0].recommendation.recommendation_score).toBe(0);
  });

  it("accepts a null fine category and OTHER as distinct values", async () => {
    stub(withItems([item(2, 1, 0.5, null), item(5, 2, 0.5, "OTHER")]));
    const items = (await getRecommendation()).current_run!.items;
    expect(items[0].opportunity.fine_primary_category).toBeNull();
    expect(items[0].opportunity.fine_classifier_version).toBeNull();
    expect(items[1].opportunity.fine_primary_category).toBe("OTHER");
  });

  it("accepts an arbitrary explanation object without parsing it", async () => {
    const odd = item(2, 1, 0.5);
    odd.recommendation.explanation = { whatever: { deep: [{ a: true }] } } as never;
    stub(withItems([odd]));
    expect((await getRecommendation()).current_run!.items[0].recommendation.explanation).toEqual({ whatever: { deep: [{ a: true }] } });
  });

  const base = item(2, 1, 0.5);
  it.each([
    ["an unknown status", { ...ready, status: "BROKEN" }],
    ["a non-object payload", "nope"],
    ["NOT_SYNCED carrying a run", { ...ready, status: "NOT_SYNCED" }],
    ["INCOMPLETE carrying a run", { ...ready, status: "INCOMPLETE" }],
    ["READY without a run", { ...ready, current_run: null }],
    ["a missing readiness_issues", { ...ready, readiness_issues: undefined }],
    ["a count that disagrees with the items", { ...ready, current_run: { ...ready.current_run, assessment_count: 5 } }],
    ["an empty item", withItems([{}])],
    ["an unknown disposition", withItems([{ ...base, recommendation: { ...base.recommendation, disposition: "MAYBE" } }])],
    ["a string score", withItems([{ ...base, recommendation: { ...base.recommendation, recommendation_score: "0.5" } }])],
    ["a missing score", withItems([{ ...base, recommendation: { ...base.recommendation, recommendation_score: undefined } }])],
    ["a null coverage", withItems([{ ...base, recommendation: { ...base.recommendation, evidence_coverage: null } }])],
    ["a non-list partition", withItems([{ ...base, recommendation: { ...base.recommendation, unknowns: "ELIGIBILITY_UNKNOWN" } }])],
    ["a non-object explanation", withItems([{ ...base, recommendation: { ...base.recommendation, explanation: [] } }])],
    ["an unknown fine category", withItems([{ ...base, opportunity: { ...base.opportunity, fine_primary_category: "ROBOTICS" } }])],
    ["a missing original URL", withItems([{ ...base, opportunity: { ...base.opportunity, original_url: undefined } }])],
    ["a non-integer rank", withItems([{ ...base, rank_position: "1" }])],
    ["a mismatched opportunity id", withItems([{ ...base, opportunity_id: 99 }])],
    ["a missing integrity", { ...ready, integrity: undefined }],
  ])("rejects %s", async (_label, payload) => {
    stub(payload);
    await expect(getRecommendation()).rejects.toThrow("invalid");
    expect(await loadRecommendation()).toBeNull();
  });
});
