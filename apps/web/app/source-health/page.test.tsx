import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/source-health", () => ({ loadSourceHealth: vi.fn() }));

import SourceHealthPage from "./page";
import { loadSourceHealth, type SourceHealthEntry } from "@/lib/source-health";

const loadSourceHealthMock = vi.mocked(loadSourceHealth);

const ANOMALY_MESSAGE =
  "0 résultat trouvé lors de 3 exécutions réussies consécutives. Défaillance possible du collecteur ou du parseur.";

const COLUMNS = [
  "Source",
  "Activée",
  "Dernière exécution",
  "Statut",
  "Éléments trouvés",
  "Nouveaux éléments",
  "Éléments pertinents",
  "Erreurs",
];

function entry(overrides: Partial<SourceHealthEntry> = {}): SourceHealthEntry {
  return {
    source_id: "test_source",
    enabled: true,
    last_run_at: "2026-08-03T09:00:00+00:00",
    status: "SUCCESS",
    items_found: 12,
    new_items: 4,
    relevant_items: null,
    error_type: null,
    error_message: null,
    zero_result_streak: 0,
    anomaly_code: null,
    anomaly_message: null,
    ...overrides,
  };
}

function listing(...entries: SourceHealthEntry[]) {
  return { items: entries, returned: entries.length };
}

async function render() {
  return renderToStaticMarkup(await SourceHealthPage());
}

describe("SourceHealthPage", () => {
  beforeEach(() => loadSourceHealthMock.mockReset());

  it("renders the eight columns required by the specification", async () => {
    loadSourceHealthMock.mockResolvedValue(listing(entry()));
    const html = await render();

    for (const column of COLUMNS) {
      expect(html).toContain(`<th scope="col">${column}</th>`);
    }
    expect(html.match(/<th scope="col">/g)).toHaveLength(COLUMNS.length);
  });

  it("renders a successful source with its real metrics", async () => {
    loadSourceHealthMock.mockResolvedValue(listing(entry()));
    const html = await render();

    expect(html).toContain("test_source");
    expect(html).toContain("SUCCESS");
    expect(html).toContain("2026-08-03T09:00:00+00:00");
    expect(html).toContain(">12<");
    expect(html).toContain(">4<");
    expect(html).toContain("Aucune");
    expect(html).toContain('data-anomaly="false"');
  });

  it("renders a failed source with its error type and redacted message", async () => {
    loadSourceHealthMock.mockResolvedValue(
      listing(
        entry({
          status: "FAILED",
          items_found: null,
          new_items: null,
          error_type: "RuntimeError",
          error_message: "board fetch rejected: api_key=[REDACTED]",
        }),
      ),
    );
    const html = await render();

    expect(html).toContain("FAILED");
    expect(html).toContain("RuntimeError");
    expect(html).toContain("board fetch rejected: api_key=[REDACTED]");
  });

  it("renders a running source without inventing its metrics", async () => {
    loadSourceHealthMock.mockResolvedValue(
      listing(entry({ status: "RUNNING", items_found: null, new_items: null })),
    );
    const html = await render();

    expect(html).toContain("RUNNING");
    expect(html).toContain("Inconnu");
    expect(html).not.toContain(">0<");
  });

  it("renders a source that never ran as never run rather than as zero", async () => {
    loadSourceHealthMock.mockResolvedValue(
      listing(
        entry({
          source_id: "never_run",
          enabled: false,
          last_run_at: null,
          status: null,
          items_found: null,
          new_items: null,
          relevant_items: null,
        }),
      ),
    );
    const html = await render();

    expect(html).toContain("never_run");
    expect(html).toContain("Non");
    expect(html).toContain("Jamais exécutée");
    expect(html).not.toContain(">0<");
    expect(html).toContain('data-anomaly="false"');
  });

  it("renders the backend anomaly message verbatim and marks the row", async () => {
    loadSourceHealthMock.mockResolvedValue(
      listing(
        entry({
          items_found: 0,
          new_items: 0,
          zero_result_streak: 3,
          anomaly_code: "ZERO_RESULTS_STREAK",
          anomaly_message: ANOMALY_MESSAGE,
        }),
      ),
    );
    const html = await render();

    expect(html).toContain('data-anomaly="true"');
    expect(html).toContain("Anomalie");
    expect(html).toContain(ANOMALY_MESSAGE);
  });

  it("shows no anomaly when the backend reported none, whatever the streak", async () => {
    loadSourceHealthMock.mockResolvedValue(
      listing(entry({ items_found: 0, new_items: 0, zero_result_streak: 2 })),
    );
    const html = await render();

    expect(html).toContain('data-anomaly="false"');
    expect(html).not.toContain("Anomalie");
    expect(html).toContain("Aucune");
  });

  it("renders unknown relevant items honestly instead of as zero", async () => {
    loadSourceHealthMock.mockResolvedValue(
      listing(entry({ items_found: 5, new_items: 2, relevant_items: null })),
    );
    const html = await render();

    expect(html).toContain("Inconnu");
    expect(html).not.toContain(">0<");
  });

  it("renders a known relevant items count of zero as zero", async () => {
    loadSourceHealthMock.mockResolvedValue(
      listing(entry({ items_found: 5, new_items: 2, relevant_items: 0 })),
    );
    const html = await render();

    expect(html).toContain(">0<");
    expect(html).not.toContain("Inconnu");
  });

  it("renders several sources without mixing their rows", async () => {
    loadSourceHealthMock.mockResolvedValue(
      listing(
        entry({
          source_id: "source_a",
          items_found: 0,
          new_items: 0,
          zero_result_streak: 3,
          anomaly_code: "ZERO_RESULTS_STREAK",
          anomaly_message: ANOMALY_MESSAGE,
        }),
        entry({ source_id: "source_b", items_found: 9, new_items: 1 }),
      ),
    );
    const html = await render();

    expect(html).toContain("2 sources connues");
    expect(html.match(/data-anomaly="true"/g)).toHaveLength(1);
    expect(html.match(/data-anomaly="false"/g)).toHaveLength(1);
    expect(html.indexOf("source_a")).toBeLessThan(html.indexOf("source_b"));
  });

  it("renders a clear empty state when no source is known", async () => {
    loadSourceHealthMock.mockResolvedValue(listing());
    const html = await render();

    expect(html).toContain("Aucune source connue pour le moment.");
    expect(html).not.toContain("source-health-table");
  });

  it("renders a safe error state without leaking technical details", async () => {
    loadSourceHealthMock.mockResolvedValue(null);
    const html = await render();

    expect(html).toContain("État des sources temporairement indisponible");
    expect(html).not.toContain("ECONNREFUSED");
    expect(html).not.toContain("source-health-table");
  });
});

describe("source health presentation boundary", () => {
  const sources = ["./page.tsx", "../../lib/source-health.ts"].map((relative) =>
    readFileSync(fileURLToPath(new URL(relative, import.meta.url)), "utf8"),
  );

  it("never compares the streak against a threshold in React", () => {
    for (const source of sources) {
      // A type guard on the field is fine; ordering it against a threshold,
      // or against any literal count, would be re-deciding the anomaly here.
      expect(source).not.toMatch(/zero_result_streak\s*(>=|<=|>|<)/);
      expect(source).not.toMatch(/(>=|<=|>|<)\s*(entry\.)?zero_result_streak/);
      expect(source).not.toMatch(/zero_result_streak\s*[=!]==?\s*\d/);
      expect(source).not.toMatch(/\d\s*[=!]==?\s*(entry\.)?zero_result_streak/);
    }
  });

  it("never rebuilds the anomaly message in React", () => {
    for (const source of sources) {
      expect(source).not.toContain("consécutives");
      expect(source).not.toContain("parseur");
    }
  });
});
