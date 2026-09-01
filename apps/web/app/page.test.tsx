import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/opportunities", () => ({ loadOpportunities: vi.fn() }));

import Home from "./page";
import { loadOpportunities } from "@/lib/opportunities";

const loadOpportunitiesMock = vi.mocked(loadOpportunities);
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
  beforeEach(() => loadOpportunitiesMock.mockReset());

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
});
