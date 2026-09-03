import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import manifest from "./manifest";

const PNG_SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);

function png(source: string): { bytes: Buffer; width: number; height: number } {
  const bytes = readFileSync(
    fileURLToPath(new URL(`../public${source}`, import.meta.url)),
  );
  return { bytes, width: bytes.readUInt32BE(16), height: bytes.readUInt32BE(20) };
}

describe("manifest icon files", () => {
  it("every declared icon exists locally at its declared size", () => {
    for (const icon of manifest().icons ?? []) {
      const file = png(icon.src);
      const [width, height] = String(icon.sizes).split("x").map(Number);

      expect(file.bytes.subarray(0, 8).equals(PNG_SIGNATURE)).toBe(true);
      expect([file.width, file.height]).toEqual([width, height]);
    }
  });
});
