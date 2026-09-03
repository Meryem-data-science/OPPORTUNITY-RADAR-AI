import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it, vi } from "vitest";

/**
 * The shipped `public/sw.js` is evaluated as-is against a fake worker scope, so
 * these tests cover the file the browser actually receives.
 */
const SOURCE = readFileSync(fileURLToPath(new URL("../public/sw.js", import.meta.url)), "utf8");

const ORIGIN = "https://radar.example.invalid";

type Listener = (event: unknown) => void;

function worker(clientUrls: string[] = []) {
  const listeners = new Map<string, Listener>();
  const showNotification =
    vi.fn<
      (title: string, options: { body: string; data: { url: string } }) => Promise<void>
    >(async () => undefined);
  const openWindow = vi.fn<(url: string) => Promise<void>>(async () => undefined);
  const clients = clientUrls.map((url) => ({
    url,
    focus: vi.fn(async () => undefined),
    navigate: vi.fn(async () => undefined),
  }));
  const self = {
    location: { origin: ORIGIN },
    registration: { showNotification },
    clients: {
      claim: vi.fn(async () => undefined),
      matchAll: vi.fn(async () => clients),
      openWindow,
    },
    skipWaiting: vi.fn(),
    addEventListener: (type: string, listener: Listener) => listeners.set(type, listener),
  };

  new Function("self", SOURCE)(self);

  async function dispatch(type: string, event: Record<string, unknown>) {
    const waits: Promise<unknown>[] = [];
    const listener = listeners.get(type);
    if (listener === undefined) throw new Error(`no listener for ${type}`);
    listener({ ...event, waitUntil: (value: Promise<unknown>) => waits.push(value) });
    await Promise.all(waits);
  }

  return { self, dispatch, showNotification, openWindow, clients, listeners };
}

function pushEvent(payload: unknown) {
  return {
    data: {
      json: () => {
        if (payload === "invalid") throw new SyntaxError("not json");
        return payload;
      },
    },
  };
}

function clickEvent(url: unknown) {
  return { notification: { data: { url }, close: vi.fn() } };
}

describe("service worker lifecycle", () => {
  it("activates immediately and claims open pages", async () => {
    const { self, dispatch, listeners } = worker();

    expect([...listeners.keys()].sort()).toEqual([
      "activate",
      "install",
      "notificationclick",
      "push",
    ]);
    await dispatch("install", {});
    expect(self.skipWaiting).toHaveBeenCalled();
    await dispatch("activate", {});
    expect(self.clients.claim).toHaveBeenCalled();
  });

  it("caches nothing: 5.3A ships no offline strategy", () => {
    expect(SOURCE).not.toContain("caches");
    expect(SOURCE).not.toContain('addEventListener("fetch"');
  });

  it("knows nothing about Priority or Portfolio scoring", () => {
    expect(SOURCE).not.toMatch(/URGENT|AMBITIOUS|priority_category|matching_lane/);
  });
});

describe("push payload handling", () => {
  it("shows the payload's title and body", async () => {
    const { dispatch, showNotification } = worker();

    await dispatch(
      "push",
      pushEvent({ title: "Nouvelle opportunité", body: "Data Engineer", url: "/priority" }),
    );

    expect(showNotification).toHaveBeenCalledTimes(1);
    const [title, options] = showNotification.mock.calls[0];
    expect(title).toBe("Nouvelle opportunité");
    expect(options.body).toBe("Data Engineer");
    expect(options.data.url).toBe("/priority");
  });

  it.each([
    ["no data at all", {}],
    ["an unparseable payload", pushEvent("invalid")],
    ["a non-object payload", pushEvent(["array"])],
    ["an empty object", pushEvent({})],
  ])("falls back to a safe notification for %s", async (_label, event) => {
    const { dispatch, showNotification } = worker();

    await dispatch("push", event);

    const [title, options] = showNotification.mock.calls[0];
    expect(title).toBe("Opportunity Radar AI");
    expect(options.data.url).toBe("/portfolio");
  });
});

describe("notificationclick target", () => {
  it.each([
    ["/priority", "/priority"],
    ["/portfolio?bucket=SAFE#top", "/portfolio?bucket=SAFE#top"],
    [`${ORIGIN}/matching`, "/matching"],
    ["https://evil.example.invalid/steal", "/portfolio"],
    ["//evil.example.invalid/steal", "/portfolio"],
    ["javascript:alert(1)", "/portfolio"],
    ["", "/portfolio"],
    [undefined, "/portfolio"],
    [42, "/portfolio"],
  ])("opens %o as %s", async (candidate, expected) => {
    const { dispatch, openWindow } = worker();

    await dispatch("notificationclick", clickEvent(candidate));

    expect(openWindow).toHaveBeenCalledWith(expected);
  });

  it("closes the notification it was clicked from", async () => {
    const { dispatch } = worker();
    const event = clickEvent("/priority");

    await dispatch("notificationclick", event);

    expect(event.notification.close).toHaveBeenCalledTimes(1);
  });

  it("focuses an existing same-origin window instead of opening a new one", async () => {
    const { dispatch, openWindow, clients } = worker([`${ORIGIN}/`]);

    await dispatch("notificationclick", clickEvent("/priority"));

    expect(clients[0].focus).toHaveBeenCalled();
    expect(clients[0].navigate).toHaveBeenCalledWith("/priority");
    expect(openWindow).not.toHaveBeenCalled();
  });

  it("ignores a window from another origin", async () => {
    const { dispatch, openWindow, clients } = worker(["https://other.example.invalid/"]);

    await dispatch("notificationclick", clickEvent("/priority"));

    expect(clients[0].focus).not.toHaveBeenCalled();
    expect(openWindow).toHaveBeenCalledWith("/priority");
  });
});
