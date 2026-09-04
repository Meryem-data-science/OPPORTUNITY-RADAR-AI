import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/applications", () => ({ loadApplication: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: () => {} }) }));

import ApplicationDetailPage from "./page";
import { loadApplication } from "@/lib/applications";
import { trackedDetail } from "@/lib/applications.fixture";

const load = vi.mocked(loadApplication);

beforeEach(() => load.mockReset());

describe("ApplicationDetailPage", () => {
  it("renders the candidature, the real link and the whole timeline", async () => {
    load.mockResolvedValue(trackedDetail as never);

    const html = renderToStaticMarkup(
      await ApplicationDetailPage({ params: Promise.resolve({ id: "7" }) }),
    );

    expect(load).toHaveBeenCalledWith(7);
    for (const value of [
      "Data Engineer",
      "Example Org",
      "https://careers.example.invalid/apply/42",
      "Candidature créée",
      "Changement de statut",
      "Suivi mis à jour",
      "Sauvegardée → Envoyée",
      "3 événements",
    ]) {
      expect(html).toContain(value);
    }
  });

  it("refuses an id that is not one without asking the API", async () => {
    const html = renderToStaticMarkup(
      await ApplicationDetailPage({ params: Promise.resolve({ id: "abc" }) }),
    );

    expect(load).not.toHaveBeenCalled();
    expect(html).toContain("Candidature introuvable");
  });

  it("reports a missing candidature rather than an empty page", async () => {
    load.mockResolvedValueOnce(null);

    const html = renderToStaticMarkup(
      await ApplicationDetailPage({ params: Promise.resolve({ id: "9" }) }),
    );

    expect(html).toContain("Candidature introuvable");
  });
});
