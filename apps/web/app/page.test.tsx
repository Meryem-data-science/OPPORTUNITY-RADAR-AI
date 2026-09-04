import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/opportunities", () => ({ loadOpportunities: vi.fn() }));
vi.mock("@/lib/applications", () => ({ loadApplications: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: () => {} }) }));

import Home from "./page";
import { loadOpportunities } from "@/lib/opportunities";
import { loadApplications } from "@/lib/applications";

const loadOpportunitiesMock = vi.mocked(loadOpportunities);
const loadApplicationsMock = vi.mocked(loadApplications);
const fixture = {
  items: [
    {
      id: 7,
      canonical_title: "Test Data Engineer",
      organization: "Fixture Company",
      location: "Paris, France",
      original_url: "https://careers.example.test/jobs/7?source=radar",
      last_seen_at: "2026-08-09T10:00:00Z",
    },
  ],
  returned: 1,
  total: 37,
};

async function renderHome() {
  return renderToStaticMarkup(await Home());
}

describe("Home", () => {
  beforeEach(() => {
    loadOpportunitiesMock.mockReset();
    loadApplicationsMock.mockReset();
    // The tracking surface being empty is the default; individual tests say
    // otherwise when they are about tracking.
    loadApplicationsMock.mockResolvedValue({ profile_id: 1, items: [], total: 0 });
  });

  it("links to the matching read surface", async () => {
    loadOpportunitiesMock.mockResolvedValue(fixture);
    const html = await renderHome();
    expect(html).toContain('href="/matching"');
    expect(html).toContain("Voir mon matching");
  });

  it("renders API opportunity fields and the exact original URL", async () => {
    loadOpportunitiesMock.mockResolvedValue(fixture);
    const html = await renderHome();

    expect(html).toContain("37 opportunités détectées");
    expect(html).toContain("Test Data Engineer");
    expect(html).toContain("Fixture Company");
    expect(html).toContain("Paris, France");
    expect(html).toContain("Voir l’offre originale");
    expect(html).toContain('href="https://careers.example.test/jobs/7?source=radar"');
    expect(html).toContain('target="_blank"');
    expect(html).toContain('rel="noreferrer"');
  });

  it("renders a clear empty state without opportunity cards", async () => {
    loadOpportunitiesMock.mockResolvedValue({ items: [], returned: 0, total: 0 });
    const html = await renderHome();

    expect(html).toContain("Aucune opportunité disponible pour le moment.");
    expect(html).not.toContain("opportunity-card");
  });

  it("renders a fallback and the original link when location is null", async () => {
    loadOpportunitiesMock.mockResolvedValue({
      ...fixture,
      items: [{ ...fixture.items[0], location: null }],
    });
    const html = await renderHome();

    expect(html).toContain("Lieu non précisé");
    expect(html).toContain('href="https://careers.example.test/jobs/7?source=radar"');
  });

  it("renders a safe error state without leaking technical details", async () => {
    loadOpportunitiesMock.mockResolvedValue(null);
    const html = await renderHome();

    expect(html).toContain("Opportunités temporairement indisponibles");
    expect(html).not.toContain("ECONNREFUSED");
    expect(html).not.toContain("secret-token");
  });

  it("offers the three real actions on every opportunity", async () => {
    loadOpportunitiesMock.mockResolvedValue(fixture);
    const html = await renderHome();

    expect(html).toContain("Sauvegarder");
    expect(html).toContain("Préparer la candidature");
    expect(html).toContain("J’ai postulé");
    expect(html).toContain('href="/applications"');
    // The existing button keeps working next to the new ones.
    expect(html).toContain('href="https://careers.example.test/jobs/7?source=radar"');
  });

  it("shows the tracked status of an opportunity that is already a candidature", async () => {
    loadOpportunitiesMock.mockResolvedValue(fixture);
    loadApplicationsMock.mockResolvedValue({
      profile_id: 1,
      total: 1,
      items: [
        {
          id: 3,
          opportunity_id: 7,
          status: "SUBMITTED",
          submitted_at: "2026-03-02 10:15:00",
          last_status_change: "2026-03-02 10:15:00",
          next_action: null,
          followup_date: null,
          notes: null,
          created_at: "2026-03-01 09:00:00",
          updated_at: "2026-03-02 10:15:00",
          opportunity: {
            id: 7,
            canonical_title: "Test Data Engineer",
            organization: "Fixture Company",
            location: "Paris, France",
            original_url: "https://careers.example.test/jobs/7?source=radar",
          },
        },
      ],
    } as never);

    expect(await renderHome()).toContain("Suivi");
  });

  it("still shows opportunities when the tracking surface is unavailable", async () => {
    loadOpportunitiesMock.mockResolvedValue(fixture);
    loadApplicationsMock.mockResolvedValue(null);

    const html = await renderHome();

    expect(html).toContain("Test Data Engineer");
    expect(html).toContain("Sauvegarder");
  });
});
