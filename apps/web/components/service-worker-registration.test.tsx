import React from "react";
import { readFileSync } from "node:fs";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/service-worker", () => ({ registerServiceWorker: vi.fn() }));

import ServiceWorkerRegistration from "./service-worker-registration";
import { registerServiceWorker } from "@/lib/service-worker";

beforeEach(() => {
  vi.clearAllMocks();
});

describe("ServiceWorkerRegistration", () => {
  it("renders nothing at all", () => {
    expect(renderToStaticMarkup(<ServiceWorkerRegistration />)).toBe("");
  });

  it("never touches the Notification API", () => {
    const requestPermission = vi.fn();
    vi.stubGlobal("Notification", { permission: "default", requestPermission });

    renderToStaticMarkup(<ServiceWorkerRegistration />);

    expect(requestPermission).not.toHaveBeenCalled();
    expect(vi.mocked(registerServiceWorker)).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it("registers from an effect, and asks for no permission to do it", () => {
    const source = readFileSync(
      new URL("./service-worker-registration.tsx", import.meta.url),
      "utf8",
    );

    expect(source).toContain("React.useEffect");
    expect(source).toContain("registerServiceWorker()");
    expect(source).not.toContain("requestPermission");
    expect(source).not.toContain("PushManager");
  });
});

describe("the root layout", () => {
  it("mounts the registration globally so every page is installable", () => {
    const layout = readFileSync(new URL("../app/layout.tsx", import.meta.url), "utf8");

    expect(layout).toContain("<ServiceWorkerRegistration />");
    expect(layout).toContain('from "@/components/service-worker-registration"');
  });
});
