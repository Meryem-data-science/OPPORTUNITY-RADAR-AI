import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/applications", () => ({ loadApplications: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: () => {} }) }));

import ApplicationsPage from "./page";
import { loadApplications } from "@/lib/applications";
import { trackedApplications } from "@/lib/applications.fixture";

const load = vi.mocked(loadApplications);

beforeEach(() => load.mockReset());

describe("ApplicationsPage", () => {
  it("renders the real candidatures the API returned", async () => {
    load.mockResolvedValue(trackedApplications as never);

    const html = renderToStaticMarkup(await ApplicationsPage());

    for (const value of [
      "Data Engineer",
      "Example Org",
      "Paris",
      "Envoyée",
      "2026-03-02 10:15:00",
      "Relancer le recruteur",
      "2026-03-16",
      "Candidature envoyée via le site carrière",
      // The link back to the real offer, exactly as the backend chose it.
      "https://careers.example.invalid/apply/42",
      "/applications/7",
    ]) {
      expect(html).toContain(value);
    }
  });

  it("offers only the statuses this phase supports", async () => {
    load.mockResolvedValue(trackedApplications as never);

    const html = renderToStaticMarkup(await ApplicationsPage());

    expect(html).toContain('value="INTERVIEW"');
    expect(html).toContain('value="WITHDRAWN"');
    expect(html).not.toContain('value="READY"');
    expect(html).not.toContain('value="DISCOVERED"');
  });

  it("reports an unavailable surface instead of inventing candidatures", async () => {
    load.mockResolvedValueOnce(null);

    const html = renderToStaticMarkup(await ApplicationsPage());

    expect(html).toContain("Candidatures temporairement indisponibles");
    expect(html).not.toContain("Data Engineer");
  });

  it("says plainly that nothing is tracked yet", async () => {
    load.mockResolvedValueOnce({ profile_id: 1, items: [], total: 0 } as never);

    const html = renderToStaticMarkup(await ApplicationsPage());

    expect(html).toContain("Aucune candidature suivie");
  });
});
