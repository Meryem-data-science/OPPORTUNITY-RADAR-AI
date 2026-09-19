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

const CONFIRMED_TARGET_STRENGTHS = ["GEOGRAPHY_MATCHED", "OPPORTUNITY_TYPE_ALLOWED"];

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
      // The page shows an item only when the persisted strengths confirm both the
      // targeted geography and the opportunity type, so that is the default here.
      strengths: overrides.strengths ?? [...CONFIRMED_TARGET_STRENGTHS],
      confirmed_gaps: overrides.gaps ?? [], unknowns: overrides.unknowns ?? [],
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
    expect(html).toContain("1 opportunité confirmée sur 1 classée");
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
      strengths: ["GEOGRAPHY_MATCHED", "OPPORTUNITY_TYPE_ALLOWED"],
      // A domain gap, not a work-mode one: a contradicted work mode routes the
      // Phase 9 disposition to OUTSIDE_PREFERENCES, so it cannot occur here.
      gaps: ["DOMAIN_OUTSIDE_PREFERENCES"],
      unknowns: ["REQUIRED_SKILLS_NOT_ALL_CONFIRMED", "FUTURE_CODE_NOT_KNOWN"],
    })]));
    const html = await render();
    const group = (kind: string) => html.match(new RegExp(`recommendation-reasons-${kind}">(.*?)</section>`))![1];
    expect(group("strengths")).toContain("Forces");
    expect(group("strengths")).toContain("La localisation correspond à la zone ciblée.");
    expect(group("strengths")).not.toMatch(/domaine|compétences/);
    expect(group("gaps")).toContain("Écarts confirmés");
    expect(group("gaps")).toContain("Le domaine est hors des préférences déclarées.");
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

  it("hides an item whose geography the recommendation did not confirm", async () => {
    loadRecommendationMock.mockResolvedValue(ready([
      item(1, 1, 0.9, { strengths: ["OPPORTUNITY_TYPE_ALLOWED"], unknowns: ["GEOGRAPHY_UNKNOWN"], disposition: "UNCERTAIN" }),
      item(2, 2, 0.8, { strengths: ["OPPORTUNITY_TYPE_ALLOWED"], gaps: ["GEOGRAPHY_OUT_OF_TARGET"] }),
      item(3, 3, 0.7),
    ]));
    const html = await render();
    expect(html).not.toContain("Role 1");
    expect(html).not.toContain("Role 2");
    expect(html).toContain("Role 3");
  });

  it("hides an item whose opportunity type the recommendation did not confirm", async () => {
    loadRecommendationMock.mockResolvedValue(ready([
      item(1, 1, 0.9, { strengths: ["GEOGRAPHY_MATCHED"], unknowns: ["OPPORTUNITY_TYPE_UNKNOWN"], disposition: "UNCERTAIN" }),
      item(2, 2, 0.8, { strengths: ["GEOGRAPHY_MATCHED"], gaps: ["OPPORTUNITY_TYPE_OUTSIDE_PREFERENCES"] }),
      item(3, 3, 0.7),
    ]));
    const html = await render();
    expect(html).not.toContain("Role 1");
    expect(html).not.toContain("Role 2");
    expect(html).toContain("Role 3");
  });

  // C. An eligibility blocker is established from the person's own Digital Twin
  // facts, so hiding it would make the visible set depend on whose CV is loaded.
  it("keeps a confirmed item with a known eligibility blocker visible and names the blocker", async () => {
    loadRecommendationMock.mockResolvedValue(ready([item(9, 1, 0.55, {
      disposition: "KNOWN_BLOCKER", gaps: ["ELIGIBILITY_KNOWN_BLOCKER"],
    })]));
    const html = await render();
    expect(html).toContain("Role 9");
    expect(html).toContain("Un obstacle connu existe");
    expect(html).toContain("recommendation-badge-known_blocker");
    // The persisted gap is still shown, in its own partition.
    const gaps = html.match(/recommendation-reasons-gaps">(.*?)<\/section>/)![1];
    expect(gaps).toContain("Un obstacle d’éligibilité connu existe.");
    // It is never relabelled as a clean recommendation.
    expect(html).not.toContain("Recommandée");
  });

  // D. KNOWN_BLOCKER outranks OUTSIDE_PREFERENCES in Phase 9, so the stronger
  // label must not smuggle back a positively contradicted work mode.
  it("hides a known blocker whose work mode was positively contradicted", async () => {
    loadRecommendationMock.mockResolvedValue(ready([
      item(1, 1, 0.9, { disposition: "KNOWN_BLOCKER", gaps: ["ELIGIBILITY_KNOWN_BLOCKER", "WORK_MODE_OUTSIDE_PREFERENCES"] }),
      item(2, 2, 0.8, { disposition: "KNOWN_BLOCKER", gaps: ["ELIGIBILITY_KNOWN_BLOCKER"] }),
    ]));
    const html = await render();
    expect(html).not.toContain("Role 1");
    expect(html).toContain("Role 2");
  });

  it("never reads an unknown work mode as a contradiction", async () => {
    loadRecommendationMock.mockResolvedValue(ready([item(7, 1, 0.5, {
      disposition: "UNCERTAIN", unknowns: ["WORK_MODE_UNKNOWN"],
    })]));
    expect(await render()).toContain("Role 7");
  });

  // E. The search itself was contradicted there, which is the person's stated
  // criteria talking rather than their CV.
  it("hides items outside preferences even when both targets are confirmed", async () => {
    loadRecommendationMock.mockResolvedValue(ready([
      item(1, 1, 0.9, { disposition: "OUTSIDE_PREFERENCES" }),
      item(3, 3, 0.7, { disposition: "RECOMMENDED" }),
      item(4, 4, 0.6, { disposition: "UNCERTAIN" }),
    ]));
    const html = await render();
    expect(html).not.toContain("Role 1");
    expect(html).toContain("Role 3");
    expect(html).toContain("Role 4");
  });

  // B. Unconfirmed or unavailable skills are evidence, never a reason to hide.
  it("keeps a confirmed item visible whatever the CV evidence says about skills", async () => {
    loadRecommendationMock.mockResolvedValue(ready([
      item(1, 1, 0.9, { disposition: "UNCERTAIN", unknowns: ["REQUIRED_SKILLS_NOT_ALL_CONFIRMED"] }),
      item(2, 2, 0.2, { disposition: "UNCERTAIN", unknowns: ["REQUIRED_SKILLS_UNAVAILABLE", "SEMANTIC_EVIDENCE_UNAVAILABLE"] }),
    ]));
    const html = await render();
    expect(html).toContain("Role 1");
    expect(html).toContain("Role 2");
    expect(html).toContain("Certaines compétences requises restent à confirmer.");
  });

  // I. The property itself: the same search, two different CV outcomes.
  it("shows the same opportunities whatever the CV produced", async () => {
    const richCv = [
      item(101, 1, 0.94, { disposition: "RECOMMENDED" }),
      item(202, 2, 0.81, { disposition: "UNCERTAIN", unknowns: ["ELIGIBILITY_UNKNOWN"] }),
      item(303, 3, 0.66, { disposition: "UNCERTAIN" }),
    ];
    const thinCv = [
      item(101, 1, 0.12, { disposition: "UNCERTAIN", unknowns: ["REQUIRED_SKILLS_NOT_ALL_CONFIRMED"] }),
      // The same opportunity, now positively blocked by this person's own facts.
      item(202, 2, null, { disposition: "KNOWN_BLOCKER", gaps: ["ELIGIBILITY_KNOWN_BLOCKER"] }),
      item(303, 3, 0.05, { disposition: "UNCERTAIN", unknowns: ["REQUIRED_SKILLS_UNAVAILABLE"] }),
    ];
    const visible = async (items: RecommendationItem[]) => {
      loadRecommendationMock.mockResolvedValue(ready(items));
      const html = await render();
      return [...html.matchAll(/data-opportunity-id="(\d+)"/g)].map((match) => match[1]);
    };

    const fromRich = await visible(richCv);
    const fromThin = await visible(thinCv);

    expect(fromRich).toEqual(["101", "202", "303"]);
    expect(fromThin).toEqual(fromRich);
  });

  it("never hides an item because its score is null or zero", async () => {
    loadRecommendationMock.mockResolvedValue(ready([
      item(1, 1, null, { disposition: "UNCERTAIN" }),
      item(2, 2, 0, { disposition: "RECOMMENDED" }),
      item(3, 3, 0, { disposition: "KNOWN_BLOCKER", gaps: ["ELIGIBILITY_KNOWN_BLOCKER"] }),
    ]));
    const html = await render();
    expect(html).toContain("Role 1");
    expect(html).toContain("Role 2");
    expect(html).toContain("Role 3");
    expect(html).toContain("Score non disponible");
  });

  it("keeps a confirmed UNCERTAIN item visible with its unknowns", async () => {
    loadRecommendationMock.mockResolvedValue(ready([item(7, 1, 0.4, {
      disposition: "UNCERTAIN", unknowns: ["REQUIRED_SKILLS_NOT_ALL_CONFIRMED"],
    })]));
    const html = await render();
    expect(html).toContain("Role 7");
    expect(html).toContain("Des incertitudes restent à lever");
    expect(html).toContain("Certaines compétences requises restent à confirmer.");
  });

  it("preserves the persisted order and rank of the confirmed items", async () => {
    loadRecommendationMock.mockResolvedValue(ready([
      item(10, 1, 0.2),
      item(20, 2, 0.95, { strengths: ["GEOGRAPHY_MATCHED"] }),
      item(30, 3, 0.9),
      item(40, 4, 0.99, { disposition: "OUTSIDE_PREFERENCES" }),
      item(50, 5, 0.1),
    ]));
    const html = await render();
    expect(html).not.toContain("Role 20");
    expect(html).not.toContain("Role 40");
    const positions = order(html, ["Role 10", "Role 30", "Role 50"]);
    expect(positions.every((position) => position >= 0)).toBe(true);
    expect(positions).toEqual([...positions].sort((a, b) => a - b));
    const ranks = order(html, ["n° 1", "n° 3", "n° 5"]);
    expect(ranks).toEqual([...ranks].sort((a, b) => a - b));
    expect(html).not.toContain("n° 2");
    expect(html).not.toContain("n° 4");
  });

  it("states how many of the ranked opportunities are confirmed", async () => {
    loadRecommendationMock.mockResolvedValue(ready([
      item(1, 1, 0.9),
      item(2, 2, 0.8, { disposition: "OUTSIDE_PREFERENCES" }),
      item(3, 3, 0.7, { strengths: [] }),
    ]));
    const html = await render();
    expect(html).toContain("1 opportunité confirmée sur 3 classées");
    expect(html).toContain("géographie ciblée");
  });

  it("explains an empty confirmed set instead of showing unconfirmed items", async () => {
    loadRecommendationMock.mockResolvedValue(ready([
      item(1, 1, 0.9, { strengths: [], disposition: "UNCERTAIN" }),
      item(2, 2, 0.8, { disposition: "OUTSIDE_PREFERENCES" }),
    ]));
    const html = await render();
    expect(html).toContain("Aucune opportunité confirmée");
    expect(html).not.toContain("recommendation-card");
    expect(html).not.toContain("Role 1");
    expect(html).not.toContain("Role 2");
  });

  it("never reads a location, a country or a type to decide what to show", async () => {
    loadRecommendationMock.mockResolvedValue(ready([
      item(1, 1, 0.9, { location: "Casablanca, Maroc", strengths: ["OPPORTUNITY_TYPE_ALLOWED"] }),
      item(2, 2, 0.8, { location: "Paris, France" }),
    ]));
    const html = await render();
    // Only the persisted evidence decides: the Moroccan item is unconfirmed, the French one is confirmed.
    expect(html).not.toContain("Role 1");
    expect(html).toContain("Role 2");
    expect(html).toContain("Paris, France");
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
