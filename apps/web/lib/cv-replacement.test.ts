import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("server-only", () => ({}));

import {
  CONFLICT_NOT_RELOADED,
  CONFLICT_RELOADED,
  afterConflict,
  allowedDecisions,
  decisionExplanation,
  differenceExplanation,
  isOpenState,
  forwardActivate,
  forwardCancel,
  forwardDecision,
  forwardOpen,
  forwardReady,
  getCurrentReview,
  loadCurrentReview,
} from "./cv-replacement";
import {
  READY_DIGEST,
  SENTINEL_READING,
  activation,
  existingAbsent,
  existingProtected,
  incomingBlocked,
  incomingNew,
  noReview,
  openReview,
  readyReview,
} from "./cv-replacement.fixture";

function json(value: unknown, status = 200): Response {
  return { ok: status < 400, status, json: async () => value } as Response;
}

describe("cv replacement server access", () => {
  beforeEach(() => {
    vi.unstubAllGlobals();
    vi.stubEnv("OPPORTUNITY_API_BASE_URL", "https://api.test/");
  });

  it("reads the current review with no-store and no profile of its own", async () => {
    const fetchMock = vi.fn().mockResolvedValue(json(openReview));
    vi.stubGlobal("fetch", fetchMock);

    await expect(getCurrentReview()).resolves.toEqual(openReview);

    const [url, options] = fetchMock.mock.calls[0];
    expect(url).toBe("https://api.test/api/cv/replacements/current");
    expect(options).toMatchObject({ cache: "no-store" });
    // The browser never picks the owner; the URL never carries one.
    expect(String(url)).not.toContain("profile");
  });

  it("accepts the absence of a review as a real answer", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json(noReview)));
    await expect(getCurrentReview()).resolves.toEqual(noReview);
  });

  it.each([
    ["a replacement with a malformed token", {
      ...openReview,
      replacement: { ...openReview.replacement, ready_review_digest: "nope" },
    }],
    ["an unknown difference", {
      ...openReview,
      entries: [{ ...incomingNew, difference: "INVENTED" }],
    }],
    ["an incoming entry naming a fact", {
      ...openReview,
      entries: [{ ...incomingNew, fact_id: 3 }],
    }],
    ["an existing entry taking an incoming answer", {
      ...openReview,
      entries: [{ ...existingAbsent, decision: "ACCEPT" }],
    }],
    ["a progress that disagrees with the entries", {
      ...openReview,
      progress: { ...openReview.progress, plan_entries: 99 },
    }],
    ["an absent review carrying entries", { ...noReview, entries: [incomingNew] }],
  ])("refuses %s", async (_label, payload) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json(payload)));
    await expect(getCurrentReview()).rejects.toThrow("invalid");
  });

  it("fails safe for HTTP errors and timeouts", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json({}, 503)));
    await expect(getCurrentReview()).rejects.toThrow("failed");
    expect(await loadCurrentReview()).toBeNull();

    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("timeout")));
    expect(await loadCurrentReview()).toBeNull();
  });

  it("sends no body at all on ready and cancel", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(json(readyReview))
      .mockResolvedValueOnce(
        json({ replacement_id: 7, effective_state: "CANCELLED" }),
      );
    vi.stubGlobal("fetch", fetchMock);

    await forwardReady(7);
    await forwardCancel(7);

    for (const [, options] of fetchMock.mock.calls) {
      expect(options).not.toHaveProperty("body");
      expect(options.headers).toBeUndefined();
      expect(options.method).toBe("POST");
    }
  });

  it("forwards the confirmed digest verbatim", async () => {
    const fetchMock = vi.fn().mockResolvedValue(json(activation));
    vi.stubGlobal("fetch", fetchMock);

    await forwardActivate(7, { review_digest: READY_DIGEST });

    const [url, options] = fetchMock.mock.calls[0];
    expect(url).toBe("https://api.test/api/cv/replacements/7/activate");
    expect(JSON.parse(options.body)).toEqual({ review_digest: READY_DIGEST });
  });

  it("forwards a decision body verbatim, correction included", async () => {
    const fetchMock = vi.fn().mockResolvedValue(json(openReview));
    vi.stubGlobal("fetch", fetchMock);
    const body = { candidate_id: 11, decision: "CORRECT", staged_value: "  x  " };

    await forwardDecision(7, body);

    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual(body);
  });

  it("keeps the class of a refusal without relaying its message", async () => {
    for (const status of [400, 404, 409]) {
      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue(json({ detail: "secret" }, status)),
      );
      await expect(forwardOpen({ extraction_id: 1 })).resolves.toEqual({
        ok: false,
        status,
      });
    }
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("down")));
    await expect(forwardOpen({ extraction_id: 1 })).resolves.toEqual({
      ok: false,
      status: 503,
    });
  });

  it("refuses a write whose payload does not match the contract", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(json({ nonsense: true })));
    await expect(forwardActivate(7, {})).resolves.toEqual({
      ok: false,
      status: 502,
    });
  });
});

describe("the review vocabulary", () => {
  it("offers only the answers the backend would accept", () => {
    expect(allowedDecisions(incomingNew)).toEqual(["ACCEPT", "REJECT", "CORRECT"]);
    // A terminal reading is only ever skipped.
    expect(allowedDecisions(incomingBlocked)).toEqual(["SKIP_BLOCKED"]);
    expect(allowedDecisions(existingAbsent)).toEqual(["KEEP", "RETIRE"]);
    // A protected fact is not this document's to retire.
    expect(allowedDecisions(existingProtected)).toEqual(["KEEP"]);
  });

  it("never presents skipping or refusing as accepting", () => {
    expect(decisionExplanation("SKIP_BLOCKED")).toContain("n’est pas accepter");
    expect(decisionExplanation("REJECT")).toContain("n’entre pas dans votre profil");
    expect(decisionExplanation("RETIRE")).toContain("rien n’est supprimé");
  });

  it("says that an absence is not a loss", () => {
    const wording = differenceExplanation("ABSENT_FROM_NEW_CV");
    expect(wording).toContain("Cela ne prouve pas");
    expect(wording).toContain("absence");
  });
});

describe("privacy of the module itself", () => {
  it("never puts a reading in a URL", async () => {
    const fetchMock = vi.fn().mockResolvedValue(json(openReview));
    vi.stubGlobal("fetch", fetchMock);
    await getCurrentReview();
    for (const [url] of fetchMock.mock.calls) {
      expect(String(url)).not.toContain(SENTINEL_READING);
    }
  });
});


describe("what a conflict leaves behind", () => {
  it("always drops the confirmation in progress", () => {
    expect(afterConflict(readyReview).confirming).toBe(false);
    expect(afterConflict(null).confirming).toBe(false);
  });

  it("shows the review it managed to re-read, and asks for it again", () => {
    const outcome = afterConflict(readyReview);

    expect(outcome.snapshot).toBe(readyReview);
    expect(outcome.blocked).toBe(false);
    expect(outcome.message).toBe(CONFLICT_RELOADED);
    expect(outcome.message).toContain("rechargée");
    expect(outcome.message).toContain("confirmez à nouveau");
  });

  it("does not claim a reload that did not happen, and blocks activation", () => {
    const outcome = afterConflict(null);

    expect(outcome.snapshot).toBeNull();
    expect(outcome.blocked).toBe(true);
    expect(outcome.message).toBe(CONFLICT_NOT_RELOADED);
    // The one sentence it must not contain: that it reloaded.
    expect(outcome.message).not.toContain("vient d’être rechargée");
    expect(outcome.message).toContain("n’a pas pu être rechargée");
    expect(outcome.message).toContain("bloquée");
  });

  it("never proposes to activate with a fresher token instead", () => {
    for (const outcome of [afterConflict(readyReview), afterConflict(null)]) {
      expect(outcome).not.toHaveProperty("review_digest");
      expect(outcome).not.toHaveProperty("retry");
    }
  });
});

describe("which states are still open", () => {
  it("counts the three open ones and nothing else", () => {
    expect(isOpenState("PREPARED")).toBe(true);
    expect(isOpenState("REVIEWING")).toBe(true);
    expect(isOpenState("READY_TO_ACTIVATE")).toBe(true);
    // A cancelled or activated attempt is not one to keep offering buttons for.
    expect(isOpenState("CANCELLED")).toBe(false);
    expect(isOpenState("ACTIVATED")).toBe(false);
  });
});
