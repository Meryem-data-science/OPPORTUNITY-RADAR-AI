import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/recommendation", () => ({ loadRecommendation: vi.fn() }));
vi.mock("@/lib/applications", () => ({ loadApplications: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: () => {} }) }));

import RecommendationPage from "./page";
import { loadRecommendation, type RecommendationItem, type RecommendationResponse } from "@/lib/recommendation";
import { loadApplications } from "@/lib/applications";

const loadRecommendationMock = vi.mocked(loadRecommendation);
const loadApplicationsMock = vi.mocked(loadApplications);

function item(id: number, rank: number, score: number | null, overrides: {
  fine?: RecommendationItem["opportunity"]["fine_primary_category"]; classified?: boolean; location?: string | null;
  strengths?: string[]; gaps?: string[]; unknowns?: string[]; coverage?: number;
  disposition?: RecommendationItem["recommendation"]["disposition"];
} = {}): RecommendationItem {
  const classified = overrides.classified ?? true;
  return {
    opportunity_id: id, rank_position: rank,
    opportunity: {
      id, canonical_title: `Role ${id}`, organization: `Org ${id}`,
      location: overrides.location === undefined ? "Paris, France" : overrides.location,
      last_seen_at: "2026-09-01T10:00:00Z", original_url: `https://careers.example.test/jobs/${id}?ref=radar&x=1`,
      fine_primary_category: overrides.fine === undefined ? "DATA_ENGINEERING" : overrides.fine,
      fine_secondary_categories: classified ? [] : null,
      fine_category_evidence: classified ? [] : null,
      fine_reasons: classified ? [] : null,
      fine_classifier_version: classified ? "fine-v1" : null,
    },
    recommendation: {
      disposition: overrides.disposition ?? "RECOMMENDED", recommendation_score: score,
      evidence_coverage: overrides.coverage ?? 0.62, assessment_fingerprint: `a-${id}`,
      strengths: overrides.strengths ?? [], confirmed_gaps: overrides.gaps ?? [], unknowns: overrides.unknowns ?? [],
      explanation: { result: {} },
    },
  };
}

function ready(items: RecommendationItem[]): RecommendationResponse {
  return {
    profile_id: 1, status: "READY", persistence_version: "p1", input_assembly_version: "i1", history_count: 3,
    readiness_issues: [], integrity: { ok: true, audit_version: "audit-v1", audit_fingerprint: "audit-fingerprint" },
    current_run: {
      run_id: 12, created_at: "2026-09-10", assessment_count: items.length, persistence_version: "p1",
      input_assembly_version: "i1", recommendation_engine_version: "engine-v1", recommendation_rules_version: "rules-v1",
      source_matching_run_id: 7, source_matching_run_fingerprint: "matching-fingerprint",
      batch_fingerprint: "batch-fingerprint", run_fingerprint: "run-fingerprint", items,
    },
  };
}

async function render() {
  return renderToStaticMarkup(await RecommendationPage());
}

function order(html: string, markers: string[]) {
  return markers.map((marker) => html.indexOf(marker));
}

const application = (opportunityId: number) => ({
  id: 3, opportunity_id: opportunityId, status: "SUBMITTED", submitted_at: "2026-03-02 10:15:00",
  last_status_change: "2026-03-02 10:15:00", next_action: null, followup_date: null, notes: null,
  created_at: "2026-03-01 09:00:00", updated_at: "2026-03-02 10:15:00",
  opportunity: { id: opportunityId, canonical_title: `Role ${opportunityId}`, organization: "Org", location: null, original_url: "https://example.invalid" },
});

describe("RecommendationPage", () => {
  beforeEach(() => {
    loadRecommendationMock.mockReset();
    loadApplicationsMock.mockReset();
    loadApplicationsMock.mockResolvedValue({ profile_id: 1, items: [], total: 0 });
  });

  it("renders a safe unavailable state without technical details", async () => {
    loadRecommendationMock.mockResolvedValue(null);
    const html = await render();
    expect(html).toContain("Recommandé pour mon CV");
    expect(html).toContain("Recommandations temporairement indisponibles");
    expect(html).not.toContain("recommendation-card");
    expect(html).not.toMatch(/ECONNREFUSED|Traceback|sqlite|SELECT|Error/i);
  });

  it("renders NOT_SYNCED without cards or fallback content", async () => {
    loadRecommendationMock.mockResolvedValue({ ...ready([]), status: "NOT_SYNCED", persistence_version: null, input_assembly_version: null, history_count: 0, current_run: null });
    const html = await render();
    expect(html).toContain("Aucune recommandation n’a encore été synchronisée.");
    expect(html).not.toContain("recommendation-card");
    expect(html).not.toContain("Sauvegarder");
    expect(html).not.toMatch(/<button|Synchroniser|Recalculer/);
  });

  it("renders INCOMPLETE with its readiness codes and no current run", async () => {
    loadRecommendationMock.mockResolvedValue({
      ...ready([]), status: "INCOMPLETE", history_count: 4, current_run: null,
      readiness_issues: [{ code: "MATCHING_VERSION_STALE", opportunity_id: null }, { code: "OPPORTUNITY_MISSING", opportunity_id: 41 }],
    });
    const html = await render();
    expect(html).toContain("Recommandation non disponible actuellement");
    expect(html).toContain("MATCHING_VERSION_STALE");
    expect(html).toContain("OPPORTUNITY_MISSING");
    expect(html).toContain("opportunité 41");
    expect(html).not.toContain("recommendation-card");
    expect(html).not.toContain("Sauvegarder");
  });

  it("renders READY cards with persisted fields and the exact original URL", async () => {
    loadRecommendationMock.mockResolvedValue(ready([item(7, 1, 0.83, { coverage: 0.5 })]));
    const html = await render();
    expect(html).toContain("1 opportunités classées");
    expect(html).toContain("n° 1");
    expect(html).toContain("Org 7");
    expect(html).toContain("Role 7");
    expect(html).toContain("Paris, France");
    expect(html).toContain("Recommandée");
    expect(html).toContain("Data engineering");
    expect(html).toContain('href="https://careers.example.test/jobs/7?ref=radar&amp;x=1"');
    expect(html).toContain('target="_blank"');
    expect(html).toContain('rel="noreferrer"');
    expect(html).toContain("run-fingerprint");
  });

  it("keeps the received order even when it disagrees with score order", async () => {
    loadRecommendationMock.mockResolvedValue(ready([item(101, 1, 0.4), item(202, 2, 0.95), item(303, 3, null)]));
    const html = await render();
    const positions = order(html, ["Role 101", "Role 202", "Role 303"]);
    expect(positions.every((position) => position >= 0)).toBe(true);
    expect(positions).toEqual([...positions].sort((a, b) => a - b));
    expect(order(html, ["40 %", "95 %", "Score non disponible"])).toEqual([...order(html, ["40 %", "95 %", "Score non disponible"])].sort((a, b) => a - b));
  });

  it("keeps the received order and the persisted rank even when ranks are not ascending", async () => {
    loadRecommendationMock.mockResolvedValue(ready([item(11, 5, 0.1), item(22, 2, 0.9), item(33, 9, 0.5)]));
    const html = await render();
    const positions = order(html, ["Role 11", "Role 22", "Role 33"]);
    expect(positions).toEqual([...positions].sort((a, b) => a - b));
    const ranks = order(html, ["n° 5", "n° 2", "n° 9"]);
    expect(ranks.every((position) => position >= 0)).toBe(true);
    expect(ranks).toEqual([...ranks].sort((a, b) => a - b));
  });

  it("falls back when the location is null", async () => {
    loadRecommendationMock.mockResolvedValue(ready([item(7, 1, 0.5, { location: null })]));
    expect(await render()).toContain("Lieu non précisé");
  });

  it("shows a null score as unavailable, never as zero", async () => {
    loadRecommendationMock.mockResolvedValue(ready([item(7, 1, null, { coverage: 0.25 })]));
    const html = await render();
    expect(html).toContain("Score non disponible");
    expect(html).not.toMatch(/Score de recommandation<\/strong><span>(0 %|0|false|NaN)/);
    expect(html).not.toContain("NaN");
    expect(html).toMatch(/Couverture des preuves<\/strong><span>25 %<\/span>/);
  });

  it("shows a real zero score as 0 %", async () => {
    loadRecommendationMock.mockResolvedValue(ready([item(7, 1, 0, { coverage: 0.8 })]));
    const html = await render();
    expect(html).toMatch(/Score de recommandation<\/strong><span>0 %<\/span>/);
    expect(html).not.toContain("Score non disponible");
  });

  it("shows evidence coverage separately from the score", async () => {
    loadRecommendationMock.mockResolvedValue(ready([item(7, 1, 0.71, { coverage: 0.33 })]));
    const html = await render();
    expect(html).toMatch(/Score de recommandation<\/strong><span>71 %<\/span>/);
    expect(html).toMatch(/Couverture des preuves<\/strong><span>33 %<\/span>/);
  });

  it("keeps strengths, confirmed gaps and unknowns in their own groups", async () => {
    loadRecommendationMock.mockResolvedValue(ready([item(7, 1, 0.5, {
      disposition: "UNCERTAIN",
      strengths: ["GEOGRAPHY_MATCHED"],
      gaps: ["WORK_MODE_OUTSIDE_PREFERENCES"],
      unknowns: ["REQUIRED_SKILLS_NOT_ALL_CONFIRMED", "FUTURE_CODE_NOT_KNOWN"],
    })]));
    const html = await render();
    const group = (kind: string) => html.match(new RegExp(`recommendation-reasons-${kind}">(.*?)</section>`))![1];
    expect(group("strengths")).toContain("Forces");
    expect(group("strengths")).toContain("La localisation correspond à la zone ciblée.");
    expect(group("strengths")).not.toMatch(/mode de travail|compétences/);
    expect(group("gaps")).toContain("Écarts confirmés");
    expect(group("gaps")).toContain("Le mode de travail est hors des préférences déclarées.");
    expect(group("gaps")).not.toMatch(/compétences|localisation/);
    expect(group("unknowns")).toContain("À confirmer");
    expect(group("unknowns")).toContain("Certaines compétences requises restent à confirmer.");
    // An unlabelled code is shown as persisted rather than given a meaning.
    expect(group("unknowns")).toContain("<code>FUTURE_CODE_NOT_KNOWN</code>");
    expect(html).toContain("Des incertitudes restent à lever");
  });

  it("renders empty partitions without inventing reasons", async () => {
    loadRecommendationMock.mockResolvedValue(ready([item(7, 1, 0.5)]));
    const html = await render();
    expect(html).toContain("Aucune force établie.");
    expect(html).toContain("Aucun écart confirmé.");
    expect(html).toContain("Aucun point à confirmer.");
  });

  it("keeps OTHER distinct from an unclassified or unretained fine category", async () => {
    loadRecommendationMock.mockResolvedValue(ready([
      item(1, 1, 0.5, { fine: "OTHER" }),
      item(2, 2, 0.5, { fine: null, classified: false }),
      item(3, 3, 0.5, { fine: null, classified: true }),
    ]));
    const html = await render();
    const category = [...html.matchAll(/Catégorie Data\/IA<\/strong><span>(.*?)<\/span>/g)].map((match) => match[1]);
    expect(category).toEqual(["Data/IA — autre catégorie", "Non classée finement", "Aucune catégorie fine retenue"]);
  });

  it("renders the existing application actions on every card", async () => {
    loadRecommendationMock.mockResolvedValue(ready([item(7, 1, 0.5), item(8, 2, 0.4)]));
    const html = await render();
    expect(html.match(/Sauvegarder/g)).toHaveLength(2);
    expect(html).toContain("Préparer la candidature");
    expect(html).toContain("J’ai postulé");
  });

  it("recognizes a tracked status by opportunity id", async () => {
    loadRecommendationMock.mockResolvedValue(ready([item(7, 1, 0.5), item(8, 2, 0.4)]));
    loadApplicationsMock.mockResolvedValue({ profile_id: 1, total: 1, items: [application(8)] } as never);
    const html = await render();
    const cards = html.split('class="recommendation-card"').slice(1);
    expect(cards[0]).not.toContain("Suivi");
    expect(cards[1]).toContain("Suivi");
  });

  it("still renders recommendations when tracking is unavailable", async () => {
    loadRecommendationMock.mockResolvedValue(ready([item(7, 1, 0.5)]));
    loadApplicationsMock.mockResolvedValue(null);
    const html = await render();
    expect(html).toContain("Role 7");
    expect(html).toContain("Sauvegarder");
    expect(html).not.toContain("indisponible");
  });
});
