import { describe, expect, it } from "vitest";

import manifest from "./manifest";

describe("PWA manifest", () => {
  it("declares an installable standalone app", () => {
    const value = manifest();

    expect(value.name).toBe("Opportunity Radar AI");
    expect(value.short_name).toBe("Radar");
    expect(value.display).toBe("standalone");
    expect(value.start_url).toBe("/");
    expect(value.scope).toBe("/");
  });

  it("ships local icons big enough to install, including a maskable one", () => {
    const icons = manifest().icons ?? [];

    expect(icons.length).toBeGreaterThan(0);
    for (const icon of icons) {
      expect(icon.src.startsWith("/icons/")).toBe(true);
      expect(icon.type).toBe("image/png");
    }
    expect(icons.map((icon) => icon.sizes)).toContain("192x192");
    expect(icons.map((icon) => icon.sizes)).toContain("512x512");
    expect(icons.some((icon) => icon.purpose === "maskable")).toBe(true);
  });
});
