import { describe, expect, it } from "vitest";

import { makeEvent } from "@/test/fixtures";

import { countByGroup, EMPTY_FILTERS, filterEvents, groupOf, mergeEvents } from "./model";

const events = [
  makeEvent({ type: "lifecycle", summary: "Run queued" }),
  makeEvent({ type: "agent_message", summary: "Reading the repository" }),
  makeEvent({ type: "tool_call", summary: "read_file(src/greeter.py)" }),
  makeEvent({
    type: "stdout",
    summary: "syntax ok",
    payload: { text: "syntax ok: src/greeter.py" },
    attempt_id: "att-2",
  }),
  makeEvent({ type: "error", severity: "error", summary: "Adapter crashed" }),
  makeEvent({
    type: "warning",
    severity: "warning",
    summary: "Output budget reached",
    node_key: "agent",
  }),
];

describe("timeline filtering", () => {
  it("returns everything with empty filters", () => {
    expect(filterEvents(events, EMPTY_FILTERS)).toHaveLength(events.length);
  });

  it("filters by event group", () => {
    const out = filterEvents(events, { ...EMPTY_FILTERS, groups: ["tools", "output"] });
    expect(out.map((e) => e.type)).toEqual(["tool_call", "stdout"]);
  });

  it("filters by minimum severity", () => {
    expect(
      filterEvents(events, { ...EMPTY_FILTERS, minSeverity: "warning" }).map((e) => e.summary),
    ).toEqual(["Adapter crashed", "Output budget reached"]);
    expect(filterEvents(events, { ...EMPTY_FILTERS, minSeverity: "error" })).toHaveLength(1);
  });

  it("searches summaries and output text case-insensitively", () => {
    expect(filterEvents(events, { ...EMPTY_FILTERS, text: "GREETER" }).map((e) => e.type)).toEqual([
      "tool_call",
      "stdout",
    ]);
  });

  it("filters by attempt and node", () => {
    expect(filterEvents(events, { ...EMPTY_FILTERS, attemptId: "att-2" })).toHaveLength(1);
    expect(filterEvents(events, { ...EMPTY_FILTERS, nodeKey: "agent" })).toHaveLength(1);
  });

  it("counts events per group and maps unknown types to lifecycle", () => {
    expect(countByGroup(events)).toMatchObject({
      lifecycle: 1,
      agent: 1,
      tools: 1,
      output: 1,
      problems: 2,
    });
    expect(groupOf("something_new")).toBe("lifecycle");
  });
});

describe("mergeEvents", () => {
  it("appends in order and de-duplicates by sequence", () => {
    const a = makeEvent();
    const b = makeEvent();
    const c = makeEvent();
    expect(mergeEvents([a], [b, c]).map((e) => e.sequence)).toEqual([
      a.sequence,
      b.sequence,
      c.sequence,
    ]);
    expect(mergeEvents([a, b, c], [b, a]).map((e) => e.sequence)).toEqual([
      a.sequence,
      b.sequence,
      c.sequence,
    ]);
    expect(mergeEvents([c], [a]).map((e) => e.sequence)).toEqual([a.sequence, c.sequence]);
  });
});
