/**
 * Service worker registration for the PWA.
 *
 * This is deliberately independent of Web Push: installability is a property
 * of the app, not of a notification opt-in, so registration only ever looks at
 * `navigator.serviceWorker` — never at `PushManager`, and never at
 * `Notification`. A browser that cannot do push still gets an installable app,
 * and a registration that fails leaves the app working exactly as before.
 */

export const SERVICE_WORKER_PATH = "/sw.js";
export const SERVICE_WORKER_SCOPE = "/";

type Scope = Record<string, unknown>;

/** Return the service worker container, or null when the browser has none. */
export function detectServiceWorkerSupport(
  scope: unknown = globalThis,
): ServiceWorkerContainer | null {
  const candidate = scope as Scope;
  const navigatorApi = candidate.navigator as Navigator | undefined;
  if (
    !navigatorApi ||
    !("serviceWorker" in navigatorApi) ||
    !navigatorApi.serviceWorker ||
    typeof navigatorApi.serviceWorker.register !== "function"
  ) {
    return null;
  }
  return navigatorApi.serviceWorker;
}

/**
 * Reuse the registration this app already made, and only register when there
 * is none yet. `enablePush` goes through here too, so a click never depends on
 * the user having installed the PWA first — and never re-registers behind the
 * registration made at mount.
 */
export async function ensureServiceWorkerRegistration(
  container: ServiceWorkerContainer,
): Promise<ServiceWorkerRegistration> {
  const existing = await container.getRegistration(SERVICE_WORKER_PATH);
  if (existing) return existing;
  return container.register(SERVICE_WORKER_PATH, { scope: SERVICE_WORKER_SCOPE });
}

/**
 * Register the service worker for this page load. Returns null when the
 * browser has no service workers or the registration fails: neither is an
 * error the app should surface, because nothing else depends on it.
 */
export async function registerServiceWorker(
  container: ServiceWorkerContainer | null = detectServiceWorkerSupport(),
): Promise<ServiceWorkerRegistration | null> {
  if (container === null) return null;
  try {
    return await ensureServiceWorkerRegistration(container);
  } catch {
    return null;
  }
}
