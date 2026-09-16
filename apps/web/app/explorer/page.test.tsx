import { readFileSync } from "node:fs";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));
vi.mock("@/lib/explorer", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/explorer")>()),
  loadExplorer: vi.fn(),
}));
vi.mock("@/lib/applications", () => ({ loadApplications: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: () => {} }) }));

import ExplorerPage from "./page";
import { loadExplorer, type ExplorerItem, type ExplorerResponse } from "@/lib/explorer";
import { loadApplications } from "@/lib/applications";

const loadExplorerMock = vi.mocked(loadExplorer);
const loadApplicationsMock = vi.mocked(loadApplications);

function item(id: number, overrides: Partial<ExplorerItem> = {}): ExplorerItem {
  return {
    opportunity_id: id,
    canonical_title: `Role ${id}`,
    organization: `Org ${id}`,
    raw_location: `TEST ONLY place ${id}`,
    original_url: `https://careers.example.test/jobs/${id}?ref=radar&x=1`,
    last_seen_at: `2026-06-1${id % 10}T10:00:00+00:00`,
    fine_primary_category: "DATA_ENGINEERING",
    opportunity_type: "INTERNSHIP",
    resolved_locations: [{ country_code: "XX", city_key: "test-city" }],
    has_unresolved_location: false,
    sources: [{ source_id: "board_a", source_type: "greenhouse" }],
    ...overrides,
  };
}

const filters = {
  countries: ["XX", "YY"],
  cities: [{ country_code: "XX", city_key: "test-city" }, { country_code: "YY", city_key: "other-city" }],
  opportunity_types: ["INTERNSHIP" as const, "JUNIOR_ROLE" as const],
  domains: ["DATA_ENGINEERING" as const, "OTHER" as const],
  sources: [{ source_id: "board_a", source_type: "greenhouse" }, { source_id: "alert_b", source_type: "gmail_linkedin_alert" }],
};

function response(items: ExplorerItem[], overrides: Partial<ExplorerResponse> = {}): ExplorerResponse {
  return { items, returned: items.length, total: items.length, limit: 24, offset: 0, available_filters: filters, ...overrides };
}

async function render(params: Record<string, string | string[] | undefined> = {}) {
  return renderToStaticMarkup(await ExplorerPage({ searchParams: Promise.resolve(params) }));
}

function cards(html: string) {
  return html.split('<article class="explorer-card"').slice(1);
}

function cardIds(html: string) {
  return [...html.matchAll(/<article class="explorer-card" data-opportunity-id="(\d+)"/g)].map((match) => Number(match[1]));
}

function hrefs(html: string, text: string) {
  return [...html.matchAll(new RegExp(`href="([^"]*)"[^>]*>${text}<`, "g"))].map((match) => match[1].replaceAll("&amp;", "&"));
}

function select(html: string, name: string) {
  return html.match(new RegExp(`<select name="${name}"[^>]*>(.*?)</select>`))![1];
}

function optionValues(html: string, name: string) {
  return [...select(html, name).matchAll(/<option value="([^"]*)"/g)].map((match) => match[1]);
}

const application = (opportunityId: number) => ({
  id: 3, opportunity_id: opportunityId, status: "SUBMITTED", submitted_at: "2026-03-02 10:15:00",
  last_status_change: "2026-03-02 10:15:00", next_action: null, followup_date: null, notes: null,
  created_at: "2026-03-01 09:00:00", updated_at: "2026-03-02 10:15:00",
  opportunity: { id: opportunityId, canonical_title: `Role ${opportunityId}`, organization: "Org", location: null, original_url: "https://example.invalid" },
});

describe("ExplorerPage", () => {
  beforeEach(() => {
    loadExplorerMock.mockReset();
    loadApplicationsMock.mockReset();
    loadApplicationsMock.mockResolvedValue({ profile_id: 1, items: [], total: 0 });
  });

  it("renders the Explorer header and its navigation", async () => {
    loadExplorerMock.mockResolvedValue(response([item(1)]));
    const html = await render();
    expect(html).toContain("Explorer Data &amp; AI");
    expect(html).toContain("Exploration Data &amp; IA");
    expect(html).toContain('href="/recommendation"');
    expect(html).toContain("Recommandé pour mon CV");
    expect(html).toContain('href="/"');
  });

  it("renders a safe unavailable state without technical details", async () => {
    loadExplorerMock.mockResolvedValue(null);
    const html = await render();
    expect(html).toContain("Explorer temporairement indisponible");
    expect(html).not.toContain("explorer-card");
    expect(html).not.toContain("<form");
    expect(html).not.toMatch(/ECONNREFUSED|Traceback|sqlite|SELECT|\.db|OPPORTUNITY_API_BASE_URL/i);
  });

  it("renders a clear empty state without inventing opportunities", async () => {
    loadExplorerMock.mockResolvedValue(response([], { total: 0 }));
    const html = await render({ domain: "OTHER" });
    expect(html).toContain("Aucune opportunité ne correspond à ces filtres");
    expect(html).not.toContain("explorer-card");
  });

  it("renders cards in exactly the received order", async () => {
    // Deliberately neither id order nor last_seen_at order.
    loadExplorerMock.mockResolvedValue(response([item(7), item(2), item(9), item(4)]));
    const html = await render();
    expect(cardIds(html)).toEqual([7, 2, 9, 4]);
  });

  it("presents no ranking, score or sort control", async () => {
    loadExplorerMock.mockResolvedValue(response([item(1), item(2)]));
    const html = await render();
    expect(html).not.toMatch(/score|n° \d|rang|classement|recommandée|trier|sort/i);
    expect(html).not.toMatch(/name="(sort|order|order_by|profile_id)"/);
  });

  it("uses the exact original URL", async () => {
    loadExplorerMock.mockResolvedValue(response([item(3)]));
    const html = await render();
    expect(html).toContain('href="https://careers.example.test/jobs/3?ref=radar&amp;x=1" target="_blank" rel="noreferrer"');
  });

  it("keeps OTHER distinct from a null domain and shows an unknown type as unknown", async () => {
    loadExplorerMock.mockResolvedValue(response([
      item(1, { fine_primary_category: "OTHER" }),
      item(2, { fine_primary_category: null, opportunity_type: null }),
    ]));
    const [other, unknown] = cards(await render());
    expect(other).toContain("Data/IA — autre catégorie");
    expect(other).not.toContain("Domaine non renseigné");
    expect(unknown).toContain("Domaine non renseigné");
    expect(unknown).not.toContain("autre catégorie");
    expect(unknown).toContain("Type non renseigné");
    expect(other).toContain("Stage");
  });

  it("falls back when raw_location is null", async () => {
    loadExplorerMock.mockResolvedValue(response([item(1, { raw_location: null })]));
    expect(await render()).toContain("Lieu non précisé");
  });

  it("renders resolved locations and the unresolved and partially resolved states", async () => {
    loadExplorerMock.mockResolvedValue(response([
      item(1, { resolved_locations: [{ country_code: "XX", city_key: "test-city" }, { country_code: "YY", city_key: null }], has_unresolved_location: false }),
      item(2, { resolved_locations: [], has_unresolved_location: true, raw_location: "test-city, somewhere" }),
      item(3, { resolved_locations: [{ country_code: "YY", city_key: null }], has_unresolved_location: true }),
    ]));
    const [resolved, unresolved, partial] = cards(await render());
    expect(resolved).toContain("<li>XX · test-city</li>");
    expect(resolved).toContain("<li>YY</li>");
    expect(resolved).not.toMatch(/non résolue|partiellement/);
    expect(unresolved).toContain("Localisation non résolue");
    expect(unresolved).not.toContain("<li>XX");
    expect(partial).toContain("<li>YY</li>");
    expect(partial).toContain("Localisation partiellement résolue");
  });

  it("renders persisted sources", async () => {
    loadExplorerMock.mockResolvedValue(response([
      item(1, { sources: [{ source_id: "alert_b", source_type: "gmail_linkedin_alert" }, { source_id: "board_a", source_type: "greenhouse" }] }),
      item(2, { sources: [] }),
    ]));
    const [many, none] = cards(await render());
    expect(many).toContain("alert_b (gmail_linkedin_alert)");
    expect(many).toContain("board_a (greenhouse)");
    expect(none).toContain("Aucune source enregistrée");
  });

  it("populates the GET form from available_filters and the closed freshness vocabulary", async () => {
    loadExplorerMock.mockResolvedValue(response([item(1)]));
    const html = await render();
    const form = html.match(/<form [^>]*>/)![0];
    expect(form).toContain('method="get"');
    expect(form).toContain('action="/explorer"');
    expect(optionValues(html, "country")).toEqual(["", "XX", "YY"]);
    expect(optionValues(html, "city")).toEqual(["", "test-city", "other-city"]);
    expect(optionValues(html, "opportunity_type")).toEqual(["", "INTERNSHIP", "JUNIOR_ROLE"]);
    expect(optionValues(html, "domain")).toEqual(["", "DATA_ENGINEERING", "OTHER"]);
    expect(optionValues(html, "source")).toEqual(["", "board_a", "alert_b"]);
    expect(optionValues(html, "freshness")).toEqual(["", "24h", "7d", "30d"]);
    expect(select(html, "domain")).toContain("Data/IA — autre catégorie");
    expect(html).not.toMatch(/name="offset"/);
    expect(html).not.toMatch(/name="limit"/);
    expect(html).toContain('href="/explorer"');
  });

  it("passes only supported, valid URL parameters to the Explorer query and reflects them", async () => {
    loadExplorerMock.mockResolvedValue(response([item(1)]));
    const html = await render({
      country: "XX", city: "test-city", opportunity_type: "INTERNSHIP", domain: "OTHER",
      source: "board_a", freshness: "7d", limit: "10", offset: "0",
      profile_id: "999", sort: "score", unrelated: "x",
    });
    expect(loadExplorerMock).toHaveBeenCalledWith({
      country: "XX", city: "test-city", opportunity_type: "INTERNSHIP", domain: "OTHER",
      source: "board_a", freshness: "7d", limit: 10, offset: 0,
    });
    for (const [name, value] of [["country", "XX"], ["city", "test-city"], ["opportunity_type", "INTERNSHIP"],
      ["domain", "OTHER"], ["source", "board_a"], ["freshness", "7d"]]) {
      expect(select(html, name)).toContain(`<option value="${value}" selected="">`);
    }
    expect(html).toContain('<input type="hidden" name="limit" value="10"/>');
    expect(html).not.toMatch(/name="offset"/);
  });

  it("treats the empty values a normal form submission sends as absent filters", async () => {
    loadExplorerMock.mockResolvedValue(response([item(1)]));
    const html = await render({ country: "", city: "  ", opportunity_type: "", domain: "", source: "", freshness: "" });
    expect(loadExplorerMock).toHaveBeenCalledWith({});
    expect(cardIds(html)).toEqual([1]);
    expect(html).not.toContain("Paramètres Explorer invalides");
  });

  it("still ignores unrelated parameters", async () => {
    loadExplorerMock.mockResolvedValue(response([item(1)]));
    const html = await render({ foo: "bar", domain: "OTHER" });
    expect(loadExplorerMock).toHaveBeenCalledWith({ domain: "OTHER" });
    expect(cardIds(html)).toEqual([1]);
  });

  it.each([
    ["freshness=1y", { freshness: "1y" }],
    ["an invalid domain", { domain: "NOT_REAL" }],
    ["a lower-case domain", { domain: "other" }],
    ["an invalid opportunity type", { opportunity_type: "CDI" }],
    ["a malformed country", { country: "morocco" }],
    ["a lower-case country code", { country: "xx" }],
    ["a limit above the maximum", { limit: "999" }],
    ["a zero limit", { limit: "0" }],
    ["a non-integer limit", { limit: "2.5" }],
    ["a negative offset", { offset: "-1" }],
    ["a non-numeric offset", { offset: "abc" }],
    ["one invalid value beside valid ones", { domain: "OTHER", country: "XX", freshness: "1y" }],
  ])("refuses %s instead of rendering a weakened Explorer", async (_label, params) => {
    loadExplorerMock.mockResolvedValue(response([item(1), item(2)]));
    const html = await render({ ...params, foo: "bar" });
    expect(loadExplorerMock).not.toHaveBeenCalled();
    expect(loadApplicationsMock).not.toHaveBeenCalled();
    expect(html).toContain("Paramètres Explorer invalides");
    expect(html).toContain('href="/explorer"');
    expect(html).toContain("Explorer Data &amp; AI");
    expect(html).not.toContain("explorer-card");
    expect(html).not.toContain("<form");
    expect(html).not.toContain("correspondante");
  });

  it("leaks no technical information in the invalid-query state", async () => {
    const html = await render({ freshness: "1y", domain: "NOT_REAL", country: "morocco", limit: "999", offset: "-1" });
    expect(html).toContain("Paramètres Explorer invalides");
    expect(html).not.toMatch(/NOT_REAL|morocco|999|1y|Error|Traceback|sqlite|SELECT|422|OPPORTUNITY_API_BASE_URL|127\.0\.0\.1/i);
  });

  it("paginates with the response bounds while preserving every active filter and the limit", async () => {
    const items = [item(11), item(12)];
    loadExplorerMock.mockResolvedValue(response(items, { total: 7, limit: 2, offset: 2, returned: 2 }));
    const html = await render({ country: "XX", domain: "OTHER", source: "board_a", freshness: "30d", limit: "2", offset: "2" });
    expect(html).toContain("7 opportunités correspondantes");
    expect(html).toContain("Résultats 3–4 sur 7");
    const [previous] = hrefs(html, "Page précédente");
    const [next] = hrefs(html, "Page suivante");
    expect(previous).toBe("/explorer?country=XX&domain=OTHER&source=board_a&freshness=30d&limit=2&offset=0");
    expect(next).toBe("/explorer?country=XX&domain=OTHER&source=board_a&freshness=30d&limit=2&offset=4");
  });

  it("clamps the previous offset at zero and has no next link on the final page", async () => {
    loadExplorerMock.mockResolvedValue(response([item(1)], { total: 4, limit: 3, offset: 3, returned: 1 }));
    const html = await render({ offset: "3", limit: "3" });
    expect(hrefs(html, "Page précédente")).toEqual(["/explorer?limit=3&offset=0"]);
    expect(hrefs(html, "Page suivante")).toEqual([]);
    expect(html).toContain("Résultats 4–4 sur 4");
  });

  it("shows no pagination when everything fits on one page", async () => {
    loadExplorerMock.mockResolvedValue(response([item(1), item(2)], { total: 2 }));
    const html = await render();
    expect(html).not.toContain("explorer-pagination");
  });

  it("offers the existing application actions on every card", async () => {
    loadExplorerMock.mockResolvedValue(response([item(1), item(2), item(3)]));
    const html = await render();
    expect(html.match(/Sauvegarder/g)).toHaveLength(3);
    expect(html.match(/Préparer la candidature/g)).toHaveLength(3);
    expect(html.match(/J’ai postulé/g)).toHaveLength(3);
  });

  it("recognizes a tracked status by opportunity id", async () => {
    loadExplorerMock.mockResolvedValue(response([item(1), item(2)]));
    loadApplicationsMock.mockResolvedValue({ profile_id: 1, total: 1, items: [application(2)] } as never);
    const [first, second] = cards(await render());
    expect(first).not.toContain("Suivi");
    expect(second).toContain("Suivi");
  });

  it("still renders the Explorer when tracking is unavailable", async () => {
    loadExplorerMock.mockResolvedValue(response([item(1)]));
    loadApplicationsMock.mockResolvedValue(null);
    const html = await render();
    expect(cardIds(html)).toEqual([1]);
    expect(html).toContain("Sauvegarder");
    expect(html).not.toContain("indisponible");
  });

  it("imports no personalized or database surface and never sorts items", () => {
    for (const file of ["app/explorer/page.tsx", "lib/explorer.ts"]) {
      const source = readFileSync(file, "utf8");
      expect(source).not.toMatch(/from "@\/lib\/(recommendation|matching|priority|portfolio|database)"/);
      expect(source).not.toMatch(/libsql|sqlite/i);
      expect(source).not.toMatch(/\.sort\(|toSorted\(/);
    }
  });
});
