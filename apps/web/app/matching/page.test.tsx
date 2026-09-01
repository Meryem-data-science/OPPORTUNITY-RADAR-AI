import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/matching", () => ({ loadMatching: vi.fn() }));
import MatchingPage from "./page";
import { loadMatching } from "@/lib/matching";

const load = vi.mocked(loadMatching);
const item = (id: number, lane: string, quality: number | null, opportunityStatus = "MATCH") => ({
  opportunity_id: id,
  opportunity: { id, canonical_title: `Role ${id}`, organization: `Org ${id}`, location: "Remote", last_seen_at: "now", original_url: `https://example.invalid/${id}` },
  matching: { lane, match_quality: quality, evidence_coverage: .75, assessment_fingerprint: `fingerprint-${id}`, explanation: {
    opportunity_type: { status: opportunityStatus, reason: opportunityStatus === "MISMATCH" ? "OPPORTUNITY_TYPE_INCOMPATIBLE_WITH_PREFERENCES" : "TYPE_PREFERRED" },
    required_skill: { normalized_score: id === 2 ? .666666666667 : quality, matched_count: id === 2 ? 2 : 1, total_count: id === 2 ? 3 : 2 },
    semantic: { percentile: id === 2 ? .254716981132 : null, raw_similarity: .083877234714, status: id === 2 ? "available" : "unavailable" },
    domain: { normalized_score: id === 2 ? 1 : null, status: id === 2 ? "MATCH" : "UNKNOWN", preferred_rank: id === 2 ? 1 : null },
    supporting: {
      preferred: { ratio: null, matched_count: 0, total_count: 0 },
      context: { ratio: null, matched_count: 0, total_count: 0 },
    },
  } },
});
const ready = {
  profile_id: 3, status: "READY" as const, persistence_version: "p1", selection_version: "s1", history_count: 1,
  integrity: { ok: true, audit_version: "audit-v1", audit_fingerprint: "audit-fingerprint" },
  current_run: {
    run_id: 9, created_at: "2026-01-01", assessment_count: 3,
    persistence_version: "p1", selection_version: "s1", matching_engine_version: "engine-v1",
    matching_rules_version: "rules-v1", semantic_percentile_version: "semantic-v1",
    run_fingerprint: "run-fingerprint", batch_fingerprint: "batch-fingerprint",
    lane_counts: { PRIMARY: 1, UNCERTAIN: 1, OUTSIDE_PREFERENCES: 1 },
    items: [item(2, "PRIMARY", .643), item(5, "UNCERTAIN", null, "UNKNOWN"), item(8, "OUTSIDE_PREFERENCES", null, "MISMATCH")],
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
    expect(html).toContain("3 opportunités évaluées");
    expect(html).toContain("Dans vos préférences de type");
    expect(html).toContain("Compatibilité de type incertaine");
    expect(html).toContain("Role 2");
    expect(html).toContain("Role 8");
    expect(html).toContain("64 %");
    expect(html).toContain("Indisponible");
    expect(html).toContain("Couverture des preuves");
    expect(html).toContain("Compétences requises");
    expect(html).toContain("Sémantique");
    expect(html).toContain("Domaine");
    expect(html).toContain("Pourquoi ce résultat ?");
    expect(html).toContain("Dans vos préférences");
    expect(html).toContain("Hors préférences");
    expect(html).toContain("Compatibilité incertaine");
    expect(html).toMatch(/Type d’opportunité<\/strong><span>Hors préférences<\/span>/);
    expect(html).not.toMatch(/Type d’opportunité<\/strong><span>Indisponible<\/span>/);
    expect(html).toContain("67 %");
    expect(html).toContain("2 compétences reconnues sur 3");
    expect(html).toMatch(/Sémantique<\/strong><span>25 %<\/span>/);
    expect(html).toContain("Similarité brute : 8 %");
    expect(html).toMatch(/Domaine<\/strong><span>100 %<\/span>/);
    expect(html).toContain("Préférence de domaine n°1");
    expect(html).toContain("Non évaluable — aucune compétence préférée requise");
    expect(html).toContain("Non évaluable — aucun signal de contexte disponible");
    expect(html).not.toMatch(/Compétences préférées<\/strong><span>0 %/);
    expect(html).not.toMatch(/Compétences de contexte<\/strong><span>0 %/);
    expect(html).toContain("Données techniques");
    expect(html).toContain("run-fingerprint");
    expect(html).toContain("audit-fingerprint");
    expect(html).toContain('href="https://example.invalid/8"');
    expect(html).not.toMatch(/Top|Recommandé|#1|priority|ranking/);
  });
});
