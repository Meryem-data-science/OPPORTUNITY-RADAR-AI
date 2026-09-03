import { describe, expect, it, vi } from "vitest";

import {
  detectServiceWorkerSupport,
  ensureServiceWorkerRegistration,
  registerServiceWorker,
} from "./service-worker";

function container(options: { existing?: boolean; fails?: boolean } = {}) {
  const registration = { scope: "/", pushManager: {} } as ServiceWorkerRegistration;
  const register = vi.fn<
    (path: string, options: { scope: string }) => Promise<ServiceWorkerRegistration>
  >(async () => {
    if (options.fails) throw new Error("registration refused");
    return registration;
  });
  const getRegistration = vi.fn<
    (path: string) => Promise<ServiceWorkerRegistration | undefined>
  >(async () => (options.existing ? registration : undefined));
  return {
    registration,
    api: { register, getRegistration } as unknown as ServiceWorkerContainer,
    register,
    getRegistration,
  };
}

describe("detectServiceWorkerSupport", () => {
  it("needs only navigator.serviceWorker: no PushManager, no Notification", () => {
    const scope = { navigator: { serviceWorker: { register: () => {} } } };

    expect(detectServiceWorkerSupport(scope)).toBe(scope.navigator.serviceWorker);
  });

  it("returns null when the browser has no service workers", () => {
    expect(detectServiceWorkerSupport({})).toBeNull();
    expect(detectServiceWorkerSupport({ navigator: {} })).toBeNull();
    expect(detectServiceWorkerSupport({ navigator: { serviceWorker: {} } })).toBeNull();
  });
});

describe("registerServiceWorker", () => {
  it("registers /sw.js at the app scope without asking for any permission", async () => {
    const requestPermission = vi.fn();
    vi.stubGlobal("Notification", { permission: "default", requestPermission });
    const { api, register, registration } = container();

    await expect(registerServiceWorker(api)).resolves.toBe(registration);

    expect(register).toHaveBeenCalledWith("/sw.js", { scope: "/" });
    expect(requestPermission).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it("registers in a browser that has no PushManager and no Notification", async () => {
    const scope = { navigator: { serviceWorker: container().api } };

    expect("PushManager" in scope).toBe(false);
    expect("Notification" in scope).toBe(false);
    const support = detectServiceWorkerSupport(scope);
    expect(support).not.toBeNull();
    await expect(registerServiceWorker(support)).resolves.not.toBeNull();
  });

  it("reuses an existing registration instead of registering twice", async () => {
    const { api, register, getRegistration, registration } = container({
      existing: true,
    });

    await expect(registerServiceWorker(api)).resolves.toBe(registration);

    expect(getRegistration).toHaveBeenCalledWith("/sw.js");
    expect(register).not.toHaveBeenCalled();
  });

  it("leaves the app working when registration fails", async () => {
    const { api } = container({ fails: true });

    await expect(registerServiceWorker(api)).resolves.toBeNull();
  });

  it("is a no-op when the browser has no service workers", async () => {
    await expect(registerServiceWorker(null)).resolves.toBeNull();
  });
});

describe("ensureServiceWorkerRegistration", () => {
  it("propagates a failure so the push opt-in can report it", async () => {
    const { api } = container({ fails: true });

    await expect(ensureServiceWorkerRegistration(api)).rejects.toThrow();
  });
});
