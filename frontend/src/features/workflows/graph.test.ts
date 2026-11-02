import { describe, expect, it } from "vitest";

import type { WorkflowDefinition } from "./api";
import {
  ancestors,
  edgeLabel,
  layout,
  levels,
  newNode,
  renameKey,
  topoOrder,
  uniqueKey,
} from "./graph";

function def(keys: string[], edges: [string, string][]): WorkflowDefinition {
  return {
    nodes: keys.map((key) => ({ key, type: "transform", config: { values: {} } })),
    edges: edges.map(([from, to]) => ({ from, to })),
    inputs: [],
    settings: {},
  };
}

describe("workflow graph helpers", () => {
  it("orders nodes by dependency and detects cycles", () => {
    const diamond = def(
      ["d", "a", "b", "c"],
      [
        ["a", "b"],
        ["a", "c"],
        ["b", "d"],
        ["c", "d"],
      ],
    );
    expect(topoOrder(diamond)).toEqual(["a", "b", "c", "d"]);
    expect(
      topoOrder(
        def(
          ["a", "b"],
          [
            ["a", "b"],
            ["b", "a"],
          ],
        ),
      ),
    ).toBeNull();
  });

  it("lays parallel nodes out in the same column", () => {
    const d = def(
      ["a", "b", "c"],
      [
        ["a", "b"],
        ["a", "c"],
      ],
    );
    expect(levels(d).get("b")).toBe(1);
    const pos = layout(d);
    expect(pos.get("b")?.x).toBe(pos.get("c")?.x);
    expect(pos.get("b")?.y).not.toBe(pos.get("c")?.y);
  });

  it("keeps saved positions", () => {
    const d = def(["a"], []);
    d.nodes = d.nodes.map((n) => ({ ...n, position: { x: 5, y: 7 } }));
    expect(layout(d).get("a")).toEqual({ x: 5, y: 7 });
  });

  it("renames a key in edges and templates", () => {
    const d = def(["prep", "agent"], [["prep", "agent"]]);
    d.nodes = d.nodes.map((n) =>
      n.key === "agent"
        ? {
            ...n,
            config: { prompt: "Use {{ nodes.prep.outputs.stdout }} not {{ nodes.prepare.x }}" },
          }
        : n,
    );
    const renamed = renameKey(d, "prep", "setup");
    expect(renamed.edges[0]).toEqual({ from: "setup", to: "agent" });
    expect(renamed.nodes.find((n) => n.key === "agent")?.config.prompt).toBe(
      "Use {{ nodes.setup.outputs.stdout }} not {{ nodes.prepare.x }}",
    );
  });

  it("creates unique, valid keys", () => {
    expect(uniqueKey("agent", ["agent", "agent_2"])).toBe("agent_3");
    expect(uniqueKey("9 bad-key", [])).toBe("bad_key");
    expect(newNode("agent_run", ["agent"]).key).toBe("agent_2");
  });

  it("labels conditional edges", () => {
    expect(edgeLabel({ from: "a", to: "b" })).toBeUndefined();
    expect(edgeLabel({ from: "a", to: "b", condition: { branch: false, on: "always" } })).toBe(
      "if false, on always",
    );
  });

  it("collects every node with a path to the given key", () => {
    const d = def(
      ["a", "b", "c", "x"],
      [
        ["a", "b"],
        ["b", "c"],
      ],
    );
    expect(ancestors(d, "c")).toEqual(new Set(["a", "b"]));
  });

  it("returns an empty set for a node with no incoming edges", () => {
    const d = def(["a", "b"], [["a", "b"]]);
    expect(ancestors(d, "a")).toEqual(new Set());
  });

  it("terminates on a cycle instead of looping forever", () => {
    const d = def(
      ["a", "b"],
      [
        ["a", "b"],
        ["b", "a"],
      ],
    );
    const result = ancestors(d, "a");
    expect(result.size).toBeGreaterThan(0);
  });
});
