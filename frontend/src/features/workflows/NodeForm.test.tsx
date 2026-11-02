import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { renderWithQuery } from "@/test/fixtures";

import type { WorkflowDefinition, WorkflowNode } from "./api";
import { TYPE_HINTS } from "./graph";
import { NodeForm } from "./NodeForm";

const PROFILES = {
  items: [
    { id: "p1", name: "Demo Agent" },
    { id: "p2", name: "Strict Reviewer" },
  ],
};

function definitionWithUpstreamAgentRun(): WorkflowDefinition {
  return {
    nodes: [
      {
        key: "agent",
        type: "agent_run",
        name: "Agent",
        config: { agent_profile_id: "p1", prompt: "hi" },
      },
      {
        key: "lonely",
        type: "agent_run",
        name: "Lonely",
        config: { agent_profile_id: "p1", prompt: "hi" },
      },
      { key: "prep", type: "command", name: "Prep", config: { argv: ["git", "status"] } },
      { key: "review", type: "finding_review", name: "Finding review", config: {} },
    ],
    edges: [
      { from: "agent", to: "review" },
      { from: "prep", to: "review" },
    ],
    inputs: [],
    settings: {},
  };
}

function definitionWithNoUpstreamAgentRun(): WorkflowDefinition {
  const def = definitionWithUpstreamAgentRun();
  return { ...def, edges: def.edges.filter((e) => e.from !== "agent") };
}

function renderNodeForm(
  definition: WorkflowDefinition,
  onChange = vi.fn<(node: WorkflowNode) => void>(),
) {
  const node = definition.nodes.find((n) => n.key === "review");
  if (!node) throw new Error("fixture definition has no 'review' node");
  renderWithQuery(
    <NodeForm
      node={node}
      definition={definition}
      onChange={onChange}
      onRename={() => {}}
      onEdges={() => {}}
      onRemove={() => {}}
    />,
    { api: { "/agent-profiles": PROFILES } },
  );
  return onChange;
}

describe("NodeForm: finding_review", () => {
  it("offers only an upstream agent_run node as the target", async () => {
    renderNodeForm(definitionWithUpstreamAgentRun());
    const select = await screen.findByLabelText<HTMLSelectElement>("Target node");
    const options = Array.from(select.options).map((o) => o.value);
    expect(options).toEqual(["", "agent"]);
  });

  it("shows guidance and disables the select when there is no upstream agent run", async () => {
    renderNodeForm(definitionWithNoUpstreamAgentRun());
    const select = await screen.findByLabelText<HTMLSelectElement>("Target node");
    expect(select).toBeDisabled();
    expect(
      screen.getByText(
        "Add an edge from an agent run node first; a finding review challenges an upstream agent run's findings.",
      ),
    ).toBeInTheDocument();
  });

  it("calls onChange with the chosen target", async () => {
    const onChange = renderNodeForm(definitionWithUpstreamAgentRun());
    const select = await screen.findByLabelText("Target node");
    await userEvent.selectOptions(select, "agent");
    const last = onChange.mock.calls.at(-1)?.[0];
    expect(last?.config.target).toBe("agent");
  });

  it("calls onChange with the chosen reviewer agent", async () => {
    const onChange = renderNodeForm(definitionWithUpstreamAgentRun());
    const select = await screen.findByLabelText("Reviewer agent");
    await screen.findByRole("option", { name: "Demo Agent" });
    await userEvent.selectOptions(select, "p1");
    const last = onChange.mock.calls.at(-1)?.[0];
    expect(last?.config.agent_profile_id).toBe("p1");
  });

  it("clearing the version field removes agent_profile_version_id", async () => {
    const definition = definitionWithUpstreamAgentRun();
    definition.nodes = definition.nodes.map((n) =>
      n.key === "review" ? { ...n, config: { ...n.config, agent_profile_version_id: "v3" } } : n,
    );
    const onChange = renderNodeForm(definition);
    const field = await screen.findByLabelText("Agent version (optional)");
    await userEvent.clear(field);
    const last = onChange.mock.calls.at(-1)?.[0];
    expect(last).toBeDefined();
    expect(last?.config).not.toHaveProperty("agent_profile_version_id");
  });

  it("shows the node's hint text", async () => {
    renderNodeForm(definitionWithUpstreamAgentRun());
    expect(await screen.findByText(TYPE_HINTS.finding_review)).toBeInTheDocument();
  });
});
