import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/push-client", async () => {
  const actual = await vi.importActual<typeof import("@/lib/push-client")>(
    "@/lib/push-client",
  );
  return {
    ...actual,
    detectPushSupport: vi.fn(),
    readPushStatus: vi.fn(),
    enablePush: vi.fn(),
    disablePush: vi.fn(),
  };
});

import PushNotifications from "./push-notifications";
import {
  detectPushSupport,
  disablePush,
  enablePush,
  readPushStatus,
} from "@/lib/push-client";

const detect = vi.mocked(detectPushSupport);
const read = vi.mocked(readPushStatus);

beforeEach(() => {
  vi.clearAllMocks();
});

describe("PushNotifications", () => {
  it("renders nothing and asks for nothing before the state is known", () => {
    const requestPermission = vi.fn();
    vi.stubGlobal("Notification", { permission: "default", requestPermission });

    const html = renderToStaticMarkup(<PushNotifications />);

    expect(html).toBe("");
    expect(requestPermission).not.toHaveBeenCalled();
    expect(detect).not.toHaveBeenCalled();
    expect(read).not.toHaveBeenCalled();
    expect(vi.mocked(enablePush)).not.toHaveBeenCalled();
    expect(vi.mocked(disablePush)).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
  });

  it("only ever requests permission from the enable action", async () => {
    const source = await import("node:fs").then(({ readFileSync }) =>
      readFileSync(new URL("./push-notifications.tsx", import.meta.url), "utf8"),
    );

    expect(source).not.toContain("requestPermission");
    expect(source).toContain("run(enablePush)");
    expect(source).toContain("run(disablePush)");
  });
});
