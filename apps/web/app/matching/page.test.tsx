import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/matching", () => ({ loadMatching: vi.fn() }));
import MatchingPage from "./page";
import { loadMatching } from "@/lib/matching";

const load = vi.mocked(loadMatching);
const item = (id: number, lane: string, quality: number | null) => ({
  opportunity_id: id,
  opportunity: { id, canonical_title: `Role ${id}`, organization: `Org ${id}`, location: "Remote", last_seen_at: "now", original_url: `https://example.invalid/${id}` },
  matching: { lane, match_quality: quality, evidence_coverage: .75, assessment_fingerprint: `fingerprint-${id}`, explanation: {
    opportunity_type: { status: "MATCH", reason: "TYPE_PREFERRED" },
    required_skill: { normalized_score: quality, matched_count: 1, total_count: 2 },
    semantic: { normalized_score: quality, raw_similarity: .2 },
    domain: { normalized_score: null, status: "UNKNOWN", preferred_rank: null },
  } },
});
const ready = {
  profile_id: 3, status: "READY" as const, persistence_version: "p1", selection_version: "s1", history_count: 1,
  integrity: { ok: true, audit_version: "audit-v1", audit_fingerprint: "audit-fingerprint" },
  current_run: {
    run_id: 9, created_at: "2026-01-01", assessment_count: 2,
    persistence_version: "p1", selection_version: "s1", matching_engine_version: "engine-v1",
    matching_rules_version: "rules-v1", semantic_percentile_version: "semantic-v1",
    run_fingerprint: "run-fingerprint", batch_fingerprint: "batch-fingerprint",
    lane_counts: { PRIMARY: 1, UNCERTAIN: 0, OUTSIDE_PREFERENCES: 1 },
    items: [item(2, "PRIMARY", .643), item(8, "OUTSIDE_PREFERENCES", null)],
  },
};

describe("MatchingPage", () => {
  beforeEach(() => load.mockReset());
  it("renders unavailable, not-synced, and empty states without write controls", async () => {
    load.mockResolvedValueOnce(null);
    expect(renderToStaticMarkup(await MatchingPage())).toContain("Matching temporairement indisponible");
    load.mockResolvedValueOnce({ ...ready, status: "NOT_SYNCED", current_run: null });
    const notSynced = renderToStaticMarkup(await MatchingPage());
    expect(notSynced).toContain("Matching non encore calculé");
    expect(notSynced).not.toMatch(/<button|Recalculer|Synchroniser/);
    load.mockResolvedValueOnce({ ...ready, status: "EMPTY", current_run: null });
    expect(renderToStaticMarkup(await MatchingPage())).toContain("Aucune opportunité dans le périmètre");
  });
  it("renders all lanes, persisted scores, explanations, links, and provenance", async () => {
    load.mockResolvedValue(ready);
    const html = renderToStaticMarkup(await MatchingPage());
    expect(html).toContain("2 opportunités évaluées");
    expect(html).toContain("Dans vos préférences de type");
    expect(html).toContain("Compatibilité de type incertaine");
    expect(html).toContain("Aucune opportunité dans cette section");
    expect(html).toContain("Role 2");
    expect(html).toContain("Role 8");
    expect(html).toContain("64 %");
    expect(html).toContain("Indisponible");
    expect(html).toContain("Couverture des preuves");
    expect(html).toContain("Compétences requises");
    expect(html).toContain("Sémantique");
    expect(html).toContain("Domaine");
    expect(html).toContain("Pourquoi ce résultat ?");
    expect(html).toContain("run-fingerprint");
    expect(html).toContain("audit-fingerprint");
    expect(html).toContain('href="https://example.invalid/8"');
    expect(html).not.toMatch(/Top|Recommandé|#1/);
  });
});
