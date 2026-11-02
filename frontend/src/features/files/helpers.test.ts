import { describe, expect, it } from "vitest";

import { EMPTY_FILTERS, ledgerQuery, type RunFile } from "./api";
import {
  classificationTone,
  dirname,
  groupFiles,
  policyWording,
  protectedLabel,
  shortHash,
} from "./helpers";

function file(key: string, extra: Partial<RunFile> = {}): RunFile {
  return {
    file_key: key,
    classification: "modified",
    label: "Modified",
    sources: ["agent_hook"],
    first_step: null,
    last_step: null,
    is_collapsed: false,
    ...extra,
  } as RunFile;
}

describe("ledgerQuery", () => {
  it("sends only active filters and always the sort", () => {
    expect(ledgerQuery(EMPTY_FILTERS)).toEqual({ sort: "path", order: "asc" });
    expect(
      ledgerQuery({ ...EMPTY_FILTERS, q: " greet ", protected: true, classification: "created" }),
    ).toEqual({
      sort: "path",
      order: "asc",
      q: "greet",
      protected: "true",
      classification: "created",
    });
  });
});

describe("groupFiles", () => {
  const rows = [
    file("src/a.py", { sources: ["agent_hook", "git_end"], first_step: "build" }),
    file("README.md", { classification: "restored", label: "Restored" }),
    file("node_modules/", { is_collapsed: true, label: "Collapsed noisy activity" }),
  ];

  it("keeps everything in one group when not grouping", () => {
    expect(groupFiles(rows, "none")).toHaveLength(1);
  });

  it("groups by directory, step, operation, and source", () => {
    expect(groupFiles(rows, "directory").map((g) => g.label)).toEqual([
      "src/",
      "Repository root",
      "node_modules/",
    ]);
    expect(groupFiles(rows, "step").map((g) => [g.label, g.rows.length])).toEqual([
      ["build", 1],
      ["No workflow step", 2],
    ]);
    expect(groupFiles(rows, "classification").map((g) => g.label)).toEqual([
      "Modified",
      "Restored",
      "Collapsed noisy activity",
    ]);
    const bySource = groupFiles(rows, "source");
    expect(bySource.find((g) => g.key === "git_end")?.rows).toHaveLength(1);
    expect(bySource.find((g) => g.key === "agent_hook")?.rows).toHaveLength(3);
  });
});

describe("small helpers", () => {
  it("formats tones, hashes, directories, and policy wording", () => {
    expect(classificationTone({ classification: "created", is_collapsed: false })).toBe("success");
    expect(classificationTone({ classification: "created", is_collapsed: true })).toBe("neutral");
    expect(shortHash("0123456789abcdef")).toBe("0123456789");
    expect(shortHash(null)).toBe("");
    expect(dirname("a/b/c.txt")).toBe("a/b/");
    expect(dirname("top.txt")).toBe(".");
    expect(policyWording({ decision: "approved" })).toBe("approved");
    expect(policyWording({ decision: "detected" })).toBe("detected, not prevented");
    expect(policyWording(null)).toBe("detected, not prevented");
  });

  it("claims detection only for protected files that were written", () => {
    const base = { policy: null, operation_counts: {}, net_status: "unchanged" };
    expect(protectedLabel({ ...base, operation_counts: { search: 1 } })).toBe("Protected path");
    expect(protectedLabel({ ...base, operation_counts: { modify: 1 } })).toBe(
      "Protected, detected, not prevented",
    );
    expect(protectedLabel({ ...base, net_status: "modified" })).toBe(
      "Protected, detected, not prevented",
    );
    expect(protectedLabel({ ...base, policy: { decision: "approved" } })).toBe(
      "Protected, approved",
    );
  });
});
