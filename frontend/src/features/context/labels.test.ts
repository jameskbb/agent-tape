import { describe, expect, it } from "vitest";

import { DESTINATIONS, STAGE_INFO, freshnessLabel, plural, shortVersion } from "./labels";

describe("context labels", () => {
  it("never describes a stage as the model using or attending to something", () => {
    for (const info of Object.values(STAGE_INFO)) {
      expect(info.help).not.toMatch(/used in reasoning|attended|reasoned from|paid attention/i);
    }
  });

  it("keeps snapshot and live freshness distinct", () => {
    expect(
      freshnessLabel({
        freshness: "snapshot",
        live: false,
        synced_at: "2026-09-01",
        version: "v2",
      }),
    ).toBe("Snapshot, synced 2026-09-01, version v2");
    expect(freshnessLabel({ freshness: "live", live: true, synced_at: null, version: null })).toBe(
      "Live at query time",
    );
    expect(
      freshnessLabel({ freshness: "unknown", live: null, synced_at: null, version: null }),
    ).toBe("Freshness unknown");
  });

  it("only project instructions and prompt revisions are injected", () => {
    expect(DESTINATIONS.filter((d) => d.injects).map((d) => d.value)).toEqual([
      "project_instruction",
      "prompt_revision",
    ]);
  });

  it("formats counts and versions", () => {
    expect(plural(1, "source")).toBe("1 source");
    expect(plural(2, "source")).toBe("2 sources");
    expect(shortVersion("f9a36d4b87f89293372e41d7b032c34e9b07c7c4")).toBe("f9a36d4b87");
    expect(shortVersion("v3")).toBe("v3");
    expect(shortVersion(null)).toBeNull();
  });
});
