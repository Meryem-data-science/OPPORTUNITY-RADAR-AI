/**
 * Browser-side Web Push opt-in.
 *
 * Every call here goes to this app's own origin, and permission is only ever
 * requested from `enablePush`, which the UI calls from a click. Nothing in
 * this module runs on render.
 */

export type PushStatus =
  | "unsupported"
  | "not-configured"
  | "denied"
  | "inactive"
  | "active"
  | "error";

export type PushSupport = {
  serviceWorker: ServiceWorkerContainer;
  notification: {
    permission: NotificationPermission;
    requestPermission: () => Promise<NotificationPermission>;
  };
  fetch: typeof fetch;
};

export const CONFIG_PATH = "/api/push/config";
export const SUBSCRIPTIONS_PATH = "/api/push/subscriptions";
export const SERVICE_WORKER_PATH = "/sw.js";

type Scope = Record<string, unknown>;

/**
 * Decode a base64url VAPID application server key into the raw bytes
 * `PushManager.subscribe` expects.
 */
export function urlBase64ToUint8Array(value: string): Uint8Array<ArrayBuffer> {
  if (typeof value !== "string" || !/^[A-Za-z0-9_-]+$/.test(value)) {
    throw new Error("Invalid application server key");
  }
  const padded = value.padEnd(value.length + ((4 - (value.length % 4)) % 4), "=");
  const binary = atob(padded.replace(/-/g, "+").replace(/_/g, "/"));
  const bytes = new Uint8Array(new ArrayBuffer(binary.length));
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes;
}

/** Return the browser APIs this feature needs, or null when any is missing. */
export function detectPushSupport(scope: unknown = globalThis): PushSupport | null {
  const candidate = scope as Scope;
  const navigatorApi = candidate.navigator as Navigator | undefined;
  const notification = candidate.Notification as PushSupport["notification"] | undefined;
  const fetchApi = candidate.fetch as typeof fetch | undefined;
  if (
    !navigatorApi ||
    !("serviceWorker" in navigatorApi) ||
    !navigatorApi.serviceWorker ||
    typeof candidate.PushManager !== "function" ||
    !notification ||
    typeof notification.requestPermission !== "function" ||
    typeof fetchApi !== "function"
  ) {
    return null;
  }
  return {
    serviceWorker: navigatorApi.serviceWorker,
    notification,
    fetch: fetchApi.bind(candidate),
  };
}

async function loadConfig(support: PushSupport): Promise<string | null> {
  const response = await support.fetch(CONFIG_PATH, { cache: "no-store" });
  if (!response.ok) return null;
  const payload: unknown = await response.json();
  if (typeof payload !== "object" || payload === null) return null;
  const config = payload as { configured?: unknown; vapid_public_key?: unknown };
  if (config.configured !== true || typeof config.vapid_public_key !== "string") {
    return null;
  }
  return config.vapid_public_key;
}

function credentials(subscription: PushSubscription): unknown {
  const payload = subscription.toJSON() as {
    endpoint?: string;
    keys?: { p256dh?: string; auth?: string };
  };
  return {
    endpoint: payload.endpoint,
    keys: { p256dh: payload.keys?.p256dh, auth: payload.keys?.auth },
  };
}

async function existingSubscription(
  support: PushSupport,
): Promise<PushSubscription | null> {
  const registration = await support.serviceWorker.getRegistration(
    SERVICE_WORKER_PATH,
  );
  if (!registration) return null;
  return registration.pushManager.getSubscription();
}

/** Read the current state without ever asking for permission. */
export async function readPushStatus(support: PushSupport | null): Promise<PushStatus> {
  if (support === null) return "unsupported";
  try {
    if ((await loadConfig(support)) === null) return "not-configured";
    if (support.notification.permission === "denied") return "denied";
    if (support.notification.permission !== "granted") return "inactive";
    return (await existingSubscription(support)) === null ? "inactive" : "active";
  } catch {
    return "error";
  }
}

/**
 * Turn notifications on. Only call this from an explicit user gesture: it is
 * the single place that asks the browser for the Notification permission.
 */
export async function enablePush(support: PushSupport | null): Promise<PushStatus> {
  if (support === null) return "unsupported";
  let created: PushSubscription | null = null;
  try {
    const key = await loadConfig(support);
    if (key === null) return "not-configured";
    if (support.notification.permission === "denied") return "denied";
    if (support.notification.permission !== "granted") {
      const permission = await support.notification.requestPermission();
      if (permission !== "granted") return "denied";
    }

    const registration = await support.serviceWorker.register(SERVICE_WORKER_PATH, {
      scope: "/",
    });
    let subscription = await registration.pushManager.getSubscription();
    if (subscription === null) {
      subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(key),
      });
      created = subscription;
    }

    const response = await support.fetch(SUBSCRIPTIONS_PATH, {
      method: "POST",
      cache: "no-store",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(credentials(subscription)),
    });
    if (!response.ok) {
      // The server is the record of truth: a subscription it refused must not
      // survive in the browser either, so roll back the one we just created.
      await rollback(created);
      return "error";
    }
    return "active";
  } catch {
    await rollback(created);
    return "error";
  }
}

async function rollback(subscription: PushSubscription | null): Promise<void> {
  if (subscription === null) return;
  try {
    await subscription.unsubscribe();
  } catch {
    // Nothing more to undo: the caller already reports the failure.
  }
}

/** Turn notifications off: revoke on the server first, then in the browser. */
export async function disablePush(support: PushSupport | null): Promise<PushStatus> {
  if (support === null) return "unsupported";
  try {
    const subscription = await existingSubscription(support);
    if (subscription === null) return "inactive";
    const response = await support.fetch(SUBSCRIPTIONS_PATH, {
      method: "DELETE",
      cache: "no-store",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ endpoint: subscription.endpoint }),
    });
    if (!response.ok) return "error";
    await subscription.unsubscribe();
    return "inactive";
  } catch {
    return "error";
  }
}
