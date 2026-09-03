"use client";

import React from "react";
import { registerServiceWorker } from "@/lib/service-worker";

/**
 * Registers the service worker once per page load and renders nothing.
 *
 * It is mounted in the root layout so every page is installable, and it asks
 * the browser for no permission at all: the Notification prompt belongs to the
 * push opt-in and to nothing else.
 */
export default function ServiceWorkerRegistration() {
  React.useEffect(() => {
    void registerServiceWorker();
  }, []);

  return null;
}
