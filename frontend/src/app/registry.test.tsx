import { describe, expect, it } from "vitest";

import {
  collectFeatures,
  collectRunDetailActions,
  collectRunDetailTabs,
  features,
  navEntries,
  runDetailActions,
  runDetailTabs,
} from "./registry";

describe("feature registry", () => {
  it("collects every nav section in order", () => {
    expect(navEntries(features).map((n) => n.label)).toEqual([
      "Overview",
      "Findings",
      "Runs",
      "Workflows",
      "Schedules",
      "Prompts",
      "Agents",
      "Projects",
      "File Activity",
      "Context",
      "Approvals",
      "Settings",
    ]);
  });

  it("collects run-detail tabs from every feature folder, ordered", () => {
    // Core tabs keep their relative order; feature tracks may add their own tabs.
    const core = ["timeline", "attempts", "inputs", "changes", "artifacts", "evaluations"];
    const ids = runDetailTabs.map((t) => t.id);
    expect(ids.filter((id) => core.includes(id))).toEqual(core);
    expect(ids).toContain("limits");
    expect(ids).toContain("lineage");
    expect(ids).toContain("workflow");
  });

  it("lets a later module replace a tab with the same id", () => {
    const tabs = collectRunDetailTabs({
      "a/runDetailTabs.tsx": {
        runDetailTabs: [{ id: "x", label: "Old", order: 1, render: () => null }],
      },
      "b/runDetailTabs.tsx": {
        runDetailTabs: [{ id: "x", label: "New", order: 1, render: () => null }],
      },
    });
    expect(tabs.map((t) => t.label)).toEqual(["New"]);
  });

  it("collects run-detail actions ordered, later ids replacing earlier ones", () => {
    const actions = collectRunDetailActions({
      "a/runDetailActions.tsx": {
        runDetailActions: [
          { id: "b", order: 20, render: () => null },
          { id: "a", order: 10, render: () => "old" },
        ],
      },
      "b/runDetailActions.tsx": { runDetailActions: [{ id: "a", order: 10, render: () => "new" }] },
    });
    expect(actions.map((a) => a.id)).toEqual(["a", "b"]);
    expect(actions[0]?.render({ detail: {} as never })).toBe("new");
    expect(runDetailActions.map((a) => a.id)).toContain("lineage");
  });

  it("skips modules without a manifest", () => {
    expect(collectFeatures({ "x/feature.tsx": {} })).toEqual([]);
  });
});
