import React from "react";
import { readFileSync } from "node:fs";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({ useRouter: () => ({ refresh: () => {} }) }));

import ApplicationActions from "./application-actions";
import ApplicationTracking from "./application-tracking";
import { trackedApplication } from "@/lib/applications.fixture";

const actionsSource = readFileSync(
  new URL("./application-actions.tsx", import.meta.url),
  "utf8",
);
const trackingSource = readFileSync(
  new URL("./application-tracking.tsx", import.meta.url),
  "utf8",
);

describe("ApplicationActions", () => {
  it("offers exactly the three actions the backend accepts", () => {
    const html = renderToStaticMarkup(
      <ApplicationActions opportunityId={42} status={null} />,
    );

    expect(html).toContain("Sauvegarder");
    expect(html).toContain("Préparer la candidature");
    expect(html).toContain("J’ai postulé");
    expect(html).not.toContain("Prête");
  });

  it("shows the tracked status when there already is one", () => {
    const html = renderToStaticMarkup(
      <ApplicationActions opportunityId={42} status="INTERVIEW" />,
    );

    expect(html).toContain("Entretien");
  });

  it("posts to this app's own route handler and nowhere else", () => {
    expect(actionsSource).toContain('fetch("/api/applications"');
    expect(actionsSource).toContain('method: "POST"');
    for (const forbidden of ["libsql", "createClient", "127.0.0.1", "process.env"]) {
      expect(actionsSource).not.toContain(forbidden);
    }
  });
});

describe("ApplicationTracking", () => {
  it("renders the stored tracking values and only the selectable statuses", () => {
    const html = renderToStaticMarkup(
      <ApplicationTracking application={trackedApplication} />,
    );

    expect(html).toContain('value="Relancer le recruteur"');
    expect(html).toContain('value="2026-03-16"');
    expect(html).toContain("Candidature envoyée via le site carrière");
    expect(html).toContain('value="INTERVIEW"');
    expect(html).not.toContain('value="READY"');
    expect(html).not.toContain('value="DISCOVERED"');
  });

  it("sends the status and the tracking fields to two different routes", () => {
    expect(trackingSource).toContain("`/api/applications/${application.id}/status`");
    expect(trackingSource).toContain("`/api/applications/${application.id}`");
    expect(trackingSource).toContain('method: "PATCH"');
    // The tracking form never names a status, an owner, or a submission
    // instant: those are not fields this route may write.
    const form = trackingSource.slice(trackingSource.indexOf("function saveTracking"));
    for (const forbidden of ["profile_id", "submitted_at", "status:"]) {
      expect(form.slice(0, form.indexOf("const statusField"))).not.toContain(forbidden);
    }
    for (const forbidden of ["libsql", "createClient", "127.0.0.1", "process.env"]) {
      expect(trackingSource).not.toContain(forbidden);
    }
  });
});
