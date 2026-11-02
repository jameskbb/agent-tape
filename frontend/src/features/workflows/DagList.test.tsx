import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { WorkflowDefinition } from "./api";
import { DagList } from "./DagList";

const definition: Pick<WorkflowDefinition, "nodes" | "edges"> = {
  nodes: [
    { key: "approve", type: "approval", name: "Approve", config: { title: "OK?" } },
    { key: "prep", type: "command", name: "Prepare", config: { argv: ["git", "status"] } },
    { key: "agent", type: "agent_run", name: "Agent", config: {}, required: false },
  ],
  edges: [
    { from: "prep", to: "agent" },
    { from: "agent", to: "approve", condition: { on: "always" } },
  ],
};

describe("DagList (accessible view of the graph)", () => {
  it("lists nodes in dependency order with their prerequisites and statuses", () => {
    render(
      <DagList
        definition={definition}
        statuses={{ prep: "succeeded", agent: "running", approve: "waiting_for_approval" }}
      />,
    );
    const items = within(screen.getByRole("list", { name: /dependency order/ })).getAllByRole(
      "listitem",
    );
    expect(items.map((i) => i.textContent)).toEqual([
      expect.stringContaining("Prepare"),
      expect.stringContaining("Agent"),
      expect.stringContaining("Approve"),
    ]);
    expect(items[0]).toHaveTextContent("Starts first");
    expect(items[1]).toHaveTextContent("After prep, optional");
    expect(items[2]).toHaveTextContent("After agent (on always)");
    expect(items[2]).toHaveTextContent("Waiting for approval");
  });

  it("selects a node from the keyboard", async () => {
    const onSelect = vi.fn();
    render(<DagList definition={definition} onSelect={onSelect} selected="agent" />);
    const button = screen.getByRole("button", { name: /Approve/ });
    button.focus();
    await userEvent.keyboard("{Enter}");
    expect(onSelect).toHaveBeenCalledWith("approve");
    expect(screen.getByRole("button", { name: /Agent/ })).toHaveAttribute("aria-current", "true");
  });
});
