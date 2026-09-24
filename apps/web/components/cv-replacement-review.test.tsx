import React from "react";
import { readFileSync } from "node:fs";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import CvReplacementReview from "./cv-replacement-review";
import {
  SENTINEL_CORRECTION,
  SENTINEL_READING,
  noReview,
  openReview,
  readyReview,
  staleReview,
} from "@/lib/cv-replacement.fixture";

const source = readFileSync(
  new URL("./cv-replacement-review.tsx", import.meta.url),
  "utf8",
);

const render = (snapshot: Parameters<typeof CvReplacementReview>[0]["initial"]) =>
  renderToStaticMarkup(<CvReplacementReview initial={snapshot} />);

describe("with no open review", () => {
  it("says so, and presents opening one as a technical operation", () => {
    const html = render(noReview);

    expect(html).toContain("Aucune revue en cours");
    expect(html).toContain("déjà");
    expect(html).toContain("n’envoie aucun fichier");
    // No upload is offered anywhere: that is B4, not this page.
    expect(html).not.toContain('type="file"');
    expect(html).not.toContain("Téléverser");
    expect(html).toContain("cv-extraction-id");
  });

  it("does not pretend to know which extractions exist", () => {
    const html = render(noReview);
    expect(html).toContain("que cette page ne peut pas lister");
    expect(html).not.toContain("<select");
  });
});

describe("with an open review", () => {
  it("shows every reading exactly as the backend reported it", () => {
    const html = render(openReview);

    expect(html).toContain(SENTINEL_READING);
    expect(html).toContain("AbsentTestOnlyToolkit");
    expect(html).toContain("BlockedTestOnlyToolkit");
    expect(html).toContain("ProtectedTestOnlyToolkit");
  });

  it("preselects no decision and never calls silence an acceptance", () => {
    const html = render(openReview);

    expect(html).toContain("Aucune décision enregistrée");
    expect(html).toContain("une absence de réponse n’est pas une acceptation");
    // No control arrives already chosen.
    expect(html).not.toContain("checked");
    expect(html).not.toContain("selected");
  });

  it("offers only the answers each entry may actually take", () => {
    const html = render(openReview);

    expect(html).toContain("Accepter");
    expect(html).toContain("Refuser");
    expect(html).toContain("Corriger");
    expect(html).toContain("Passer");
    expect(html).toContain("Conserver");
    expect(html).toContain("Retirer du profil");
  });

  it("explains that skipping and refusing are not accepting", () => {
    const html = render(openReview);

    expect(html).toContain("Passer n’est pas accepter");
    expect(html).toContain("Cela ne prouve pas");
  });

  it("offers an explicit correction field", () => {
    const html = render(openReview);
    expect(html).toContain("Texte corrigé");
    expect(html).toContain('type="text"');
  });

  it("reports the progress and refuses to seal an incomplete review", () => {
    const html = render(openReview);

    expect(html).toContain("0 sur 4 éléments répondus");
    expect(html).toContain("4 en attente de votre décision");
    expect(html).toContain("Répondez à tous les éléments");
    // The seal button exists but cannot be pressed yet.
    expect(html).toContain("Déclarer la revue prête");
    expect(html).toMatch(/Déclarer la revue prête[\s\S]{0,40}$|disabled=""/);
  });

  it("surfaces stale decisions as an alert", () => {
    const html = render(staleReview);
    expect(html).toContain("2 décision(s) ne correspondent plus");
    expect(html).toContain('role="alert"');
  });
});

describe("with a review declared ready", () => {
  it("says plainly that nothing has been applied yet", () => {
    const html = render(readyReview);

    expect(html).toContain("Revue prête");
    expect(html).toContain("Rien n’a encore été appliqué");
    expect(html).toContain("Passer à l’activation");
  });

  it("does not offer the activation as the same act as sealing", () => {
    const html = render(readyReview);
    // The confirmation button is behind a second, deliberate step.
    expect(html).not.toContain("Confirmer l’activation du nouveau CV");
  });

  it("shows only a truncated token, never the whole review as a digest", () => {
    const html = render(readyReview);
    expect(html).toContain("Empreinte de la revue confirmée");
    expect(html).toContain("abababababab…");
  });

  it("shows a recorded correction without showing what it says", () => {
    const html = render(readyReview);

    expect(html).toContain("correction enregistrée");
    expect(html).not.toContain(SENTINEL_CORRECTION);
  });
});

describe("the component's own contract", () => {
  it("talks only to this app's own routes", () => {
    expect(source).toContain('fetch("/api/cv/replacements"');
    expect(source).toContain("/api/cv/replacements/${replacement?.replacement_id}/");
    for (const forbidden of [
      "libsql",
      "createClient",
      "127.0.0.1",
      "process.env",
      "server-only",
      "OPPORTUNITY_API_BASE_URL",
    ]) {
      expect(source).not.toContain(forbidden);
    }
  });

  it("never stores anything in the browser and never logs a payload", () => {
    for (const forbidden of [
      "localStorage",
      "sessionStorage",
      "indexedDB",
      "document.cookie",
      "console.log",
      "console.error",
      "navigator.sendBeacon",
    ]) {
      expect(source).not.toContain(forbidden);
    }
  });

  it("never puts a reading in a URL or a query string", () => {
    // The reading is rendered as JSX text, which is the whole point. What must
    // never happen is it reaching a request URL, so the fetch targets are what
    // this inspects.
    const targets = source.match(/fetch\([^,)]*/g) ?? [];
    expect(targets.length).toBeGreaterThan(0);
    for (const target of targets) {
      expect(target).not.toContain("display_value");
      expect(target).not.toContain("staged_value");
    }
    expect(source).not.toContain("searchParams");
    expect(source).not.toContain("?value=");
    expect(source).not.toContain("encodeURIComponent");
  });

  it.each(["ready", "cancel"])(
    "sends no body at all on %s",
    (route) => {
      // The options object of that one fetch call, whatever else moves around
      // it: these two routes take no body, and the backend refuses one.
      const call = new RegExp(`/${route}\\\`, \\{([^}]*)\\}`).exec(source);
      expect(call).not.toBeNull();
      const options = call![1];
      expect(options).toContain('method: "POST"');
      expect(options).not.toContain("body");
      expect(options).not.toContain("content-type");
    },
  );

  it("sends the confirmed digest and never re-reads a fresher one", () => {
    expect(source).toContain("function activate(digest: string)");
    expect(source).toContain("review_digest: digest");
    // The digest comes from the snapshot the person confirmed, passed in.
    expect(source).toContain("onClick={() => activate(digest)}");
  });

  it("does not retry by itself on a conflict, and asks again", () => {
    expect(source).toContain("if (response.status === 409)");
    expect(source).toContain("await refresh()");
    expect(source).toContain("afterConflict(");
    expect(source).not.toContain("setTimeout");
  });

  it("guards against a double click and against assuming success", () => {
    expect(source).toContain("if (busy !== null) return;");
    expect(source).toContain("disabled={busy !== null}");
    // The message is set from the answer, never from the click.
    expect(source).toContain("onSuccess(await response.json())");
  });

  it("launches no downstream synchronization of its own", () => {
    expect(source).toContain("Aucune synchronisation n’est lancée automatiquement");
    for (const forbidden of ["/api/matching", "/api/priority", "/api/portfolio"]) {
      expect(source).not.toContain(forbidden);
    }
  });
});


describe("a review that is no longer open", () => {
  // These are real renders: the closed state is reachable from a snapshot,
  // so it is asserted on the markup rather than on the source.
  const closed = (state: "CANCELLED" | "ACTIVATED") => ({
    ...readyReview,
    replacement: { ...readyReview.replacement!, effective_state: state },
  });

  it.each(["CANCELLED", "ACTIVATED"] as const)(
    "is reported as closed when the snapshot says %s",
    (state) => {
      const html = render(closed(state));

      expect(html).toContain("Revue close");
      expect(html).toContain("ne peut plus être activée");
      // None of the controls that would act on a live review remain.
      expect(html).not.toContain("Passer à l’activation");
      expect(html).not.toContain("Confirmer l’activation du nouveau CV");
      expect(html).not.toContain("Déclarer la revue prête");
      expect(html).not.toContain("Annuler la revue");
    },
  );

  it("says the answers remain readable", () => {
    expect(render(closed("CANCELLED"))).toContain("restent");
  });
});

describe("the wiring of the conflict and cancellation paths", () => {
  // NOTE: these assert the source, not a simulated click. The decision itself
  // — what a 409 leaves behind — is tested for real, as a pure function, in
  // lib/cv-replacement.test.ts. Driving the actual button here would need a
  // DOM renderer this repository does not install, and the limitation is
  // reported rather than papered over.

  it("routes every conflict through the shared transition", () => {
    expect(source).toContain("const reloaded = await refresh();");
    expect(source).toContain("afterConflict(reloaded ? snapshotRef.current : null)");
    expect(source).toContain("setConfirming(outcome.confirming)");
    expect(source).toContain("setBlocked(outcome.blocked)");
    expect(source).toContain("setMessage(outcome.message)");
  });

  it("lets the refresh report whether it actually reloaded", () => {
    expect(source).toContain("async function refresh(): Promise<boolean>");
    expect(source).toContain("if (!response.ok) return false;");
    expect(source).toContain("return true;");
  });

  it("blocks both steps while the snapshot is known to be stale", () => {
    expect(source).toContain("{ready && digest !== null && blocked && (");
    expect(source).toContain("{ready && digest !== null && !blocked && (");
    expect(source).toContain("{!ready && blocked && (");
    expect(source).toContain("{!ready && !blocked && (");
    expect(source).toContain("Recharger la revue");
  });

  it("marks a confirmed cancellation without waiting for the refresh", () => {
    const cancel = source.slice(source.indexOf("function cancel()"));
    expect(cancel).toContain("setCancelled(true)");
    // The flag is set before the refresh is even started, so a failed refresh
    // cannot leave a cancelled review on screen as if it were live.
    expect(cancel.indexOf("setCancelled(true)")).toBeLessThan(
      cancel.indexOf("void refresh()"),
    );
  });

  it("still never retries an activation by itself", () => {
    expect(source).not.toContain("setTimeout");
    expect(source).not.toContain("setInterval");
    // The only digest it ever sends is the one passed to `activate`, and that
    // function never reads a fresher one out of the snapshot to replace it.
    const activate = source.slice(
      source.indexOf("function activate(digest: string)"),
      source.indexOf("function cancel()"),
    );
    expect(activate).toContain("review_digest: digest");
    expect(activate).not.toContain("ready_review_digest");
  });
});
