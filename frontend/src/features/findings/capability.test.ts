import { describe, expect, it } from "vitest";

import { makeAdapter, makeDetail } from "@/test/fixtures";

import { adapterLabel, findingsCapability, recordsFindings } from "./capability";

const codex = makeAdapter({
  name: "codex-cli",
  display_name: "Codex CLI",
  findings: "unsupported",
});
const demo = makeAdapter({ name: "demo", display_name: "Demo Agent", findings: "scripted" });
const herdr = makeAdapter({
  name: "herdr",
  display_name: "Herdr runtime",
  findings: "per_profile",
});

describe("recordsFindings", () => {
  it.each([
    ["agent", true],
    ["observed", true],
    ["workflow", false],
    ["review", false],
  ])("a %s run: %s", (kind, expected) => {
    expect(recordsFindings({ kind })).toBe(expected);
  });
});

describe("findingsCapability", () => {
  it("is always observed for an observed session, whatever the adapter says", () => {
    const run = makeDetail({ kind: "observed", adapter_type: "codex-cli" }).run;
    expect(findingsCapability(run, [codex])).toBe("observed");
  });

  it("is cannot when the adapter's descriptor says unsupported", () => {
    const run = makeDetail({ adapter_type: "codex-cli" }).run;
    expect(findingsCapability(run, [codex])).toBe("cannot");
  });

  it("is can when the adapter records findings", () => {
    const run = makeDetail({ adapter_type: "demo" }).run;
    expect(findingsCapability(run, [demo])).toBe("can");
  });

  it("never claims cannot for a null or unknown adapter, or before the descriptors load", () => {
    expect(findingsCapability(makeDetail({ adapter_type: null }).run, [])).toBe("can");
    expect(findingsCapability(makeDetail({ adapter_type: "brand-new" }).run, [demo])).toBe("can");
    expect(findingsCapability(makeDetail({ adapter_type: "codex-cli" }).run, undefined)).toBe(
      "can",
    );
  });

  it("prefers the run's own capture over the adapter-wide value", () => {
    const run = makeDetail({ adapter_type: "demo", findings_capture: "unsupported" }).run;
    expect(findingsCapability(run, [demo])).toBe("cannot");
  });

  it.each(["mcp", "scripted"] as const)(
    "is can for a Herdr run whose own capture is %s",
    (capture) => {
      const run = makeDetail({ adapter_type: "herdr", findings_capture: capture }).run;
      expect(findingsCapability(run, [herdr])).toBe("can");
    },
  );

  it("never claims cannot for a per-profile adapter when the run carries no capture", () => {
    const run = makeDetail({ adapter_type: "herdr", findings_capture: null }).run;
    expect(findingsCapability(run, [herdr])).toBe("can");
  });
});

describe("adapterLabel", () => {
  it("names the adapter's display name", () => {
    expect(adapterLabel(makeDetail({ adapter_type: "codex-cli" }).run, [codex])).toBe("Codex CLI");
  });

  it("names the run's profile alongside a per-profile adapter", () => {
    const run = makeDetail({ adapter_type: "herdr", agent_profile_name: "codex in herdr" }).run;
    expect(adapterLabel(run, [herdr])).toBe("codex in herdr (Herdr runtime)");
  });

  it("falls back to the raw adapter type, then a generic label, before the descriptors load", () => {
    expect(adapterLabel(makeDetail({ adapter_type: "codex-cli" }).run, undefined)).toBe(
      "codex-cli",
    );
    expect(adapterLabel(makeDetail({ adapter_type: null }).run, undefined)).toBe("This adapter");
  });
});
