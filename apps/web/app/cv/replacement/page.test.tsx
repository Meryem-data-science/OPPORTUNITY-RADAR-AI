import { readFileSync } from "node:fs";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));
vi.mock("@/lib/cv-replacement", () => ({ loadCurrentReview: vi.fn() }));

import CvReplacementPage, { dynamic } from "./page";
import { loadCurrentReview } from "@/lib/cv-replacement";
import {
  SENTINEL_READING,
  noReview,
  openReview,
} from "@/lib/cv-replacement.fixture";

const load = vi.mocked(loadCurrentReview);
const source = readFileSync(new URL("./page.tsx", import.meta.url), "utf8");

describe("CvReplacementPage", () => {
  beforeEach(() => load.mockReset());

  it("is dynamic, so the review is never served from a cache", () => {
    expect(dynamic).toBe("force-dynamic");
  });

  it("reports an unusable backend without inventing a review", async () => {
    load.mockResolvedValueOnce(null);

    const html = renderToStaticMarkup(await CvReplacementPage());

    expect(html).toContain("Revue de remplacement indisponible");
    expect(html).toContain("Aucune revue n’est créée");
    // It never offers to migrate anything to make itself work.
    expect(html).not.toContain("migration");
    expect(html).not.toContain("0028");
    expect(html).not.toContain("Aucune revue en cours");
  });

  it("says plainly when there is no review, without an upload", async () => {
    load.mockResolvedValueOnce(noReview);

    const html = renderToStaticMarkup(await CvReplacementPage());

    expect(html).toContain("Aucune revue en cours");
    expect(html).not.toContain('type="file"');
  });

  it("renders the review, readings included", async () => {
    load.mockResolvedValueOnce(openReview);

    const html = renderToStaticMarkup(await CvReplacementPage());

    expect(html).toContain("Lectures à examiner");
    expect(html).toContain(SENTINEL_READING);
  });

  it("keeps the reading out of the page title and the document head", async () => {
    load.mockResolvedValueOnce(openReview);

    const html = renderToStaticMarkup(await CvReplacementPage());
    const heading = html.slice(0, html.indexOf("Lectures à examiner"));

    expect(heading).toContain("Remplacement du CV");
    expect(heading).not.toContain(SENTINEL_READING);
    expect(source).not.toContain("metadata");
    expect(source).not.toContain("title:");
  });

  it("opens no database and reaches no backend of its own", () => {
    for (const forbidden of [
      "libsql",
      "createClient",
      "@/lib/database",
      "127.0.0.1",
      "OPPORTUNITY_API_BASE_URL",
      "fetch(",
    ]) {
      expect(source).not.toContain(forbidden);
    }
    // It asks the one server module that is allowed to, and nothing else.
    expect(source).toContain('from "@/lib/cv-replacement"');
  });
});
