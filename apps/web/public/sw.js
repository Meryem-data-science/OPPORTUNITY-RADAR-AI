/*
 * Opportunity Radar AI service worker — Phase 5.3A.
 *
 * It exists so the app is installable and so a push payload has somewhere to
 * land later. It deliberately does no caching and knows nothing about Priority
 * or Portfolio: the payload it is handed decides what a notification says, and
 * the only thing this file enforces is that a click can never leave the app.
 */

const FALLBACK_URL = "/portfolio";
const DEFAULT_TITLE = "Opportunity Radar AI";

self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

/** Reduce any candidate to a same-origin path, or to the in-app fallback. */
function internalPath(candidate) {
  if (typeof candidate !== "string" || candidate.trim() === "") {
    return FALLBACK_URL;
  }
  let target;
  try {
    target = new URL(candidate, self.location.origin);
  } catch {
    return FALLBACK_URL;
  }
  if (target.origin !== self.location.origin) {
    return FALLBACK_URL;
  }
  return `${target.pathname}${target.search}${target.hash}`;
}

function readPayload(event) {
  if (!event || !event.data || typeof event.data.json !== "function") {
    return {};
  }
  let payload;
  try {
    payload = event.data.json();
  } catch {
    return {};
  }
  return payload !== null && typeof payload === "object" && !Array.isArray(payload)
    ? payload
    : {};
}

function text(value, fallback) {
  return typeof value === "string" && value.trim() !== "" ? value : fallback;
}

self.addEventListener("push", (event) => {
  const payload = readPayload(event);
  const options = {
    body: text(payload.body, ""),
    icon: "/icons/icon-192.png",
    badge: "/icons/icon-192.png",
    data: { url: internalPath(payload.url) },
  };
  if (typeof payload.tag === "string" && payload.tag.trim() !== "") {
    options.tag = payload.tag;
  }
  event.waitUntil(
    self.registration.showNotification(text(payload.title, DEFAULT_TITLE), options),
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const data = event.notification.data;
  const path = internalPath(data && data.url);
  event.waitUntil(
    (async () => {
      const windows = await self.clients.matchAll({
        type: "window",
        includeUncontrolled: true,
      });
      for (const client of windows) {
        if (new URL(client.url).origin !== self.location.origin) {
          continue;
        }
        await client.focus();
        if (typeof client.navigate === "function") {
          await client.navigate(path);
        }
        return;
      }
      await self.clients.openWindow(path);
    })(),
  );
});
