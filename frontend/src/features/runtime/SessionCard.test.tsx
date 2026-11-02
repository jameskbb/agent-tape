import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { RuntimeSession } from "./api";
import { SessionCard } from "./SessionCard";

const herdr: RuntimeSession = {
  attempt_id: "a1",
  attempt_number: 1,
  runtime: "herdr",
  machine: "local",
  session_name: "default",
  herdr_version: "0.9.0",
  protocol: 22,
  workspace_id: "w2",
  tab_id: "w2:t1",
  pane_id: "w2:p1",
  agent_name: "at-1234abcd-a1",
  agent_kind: "claude",
  native_session_ref: "reported (id)",
  lifecycle_state: "blocked",
  owned_workspace: false,
  owned_agent: true,
  last_sync_at: null,
  status: "active",
  failure_category: null,
  fallback_to: null,
  attach: {
    command: "herdr agent attach at-1234abcd-a1",
    guidance: "Run this in a terminal to open the agent in Herdr.",
  },
  capabilities: [
    { name: "attach", description: "Attach", support: "supported", note: "" },
    {
      name: "file_telemetry",
      description: "Tool-level file read/write telemetry",
      support: "unsupported",
      note: "Herdr reports lifecycle state, not tool calls",
    },
  ],
};

describe("SessionCard", () => {
  it("shows Herdr refs, ownership, lifecycle, the attach command, and honest limits", () => {
    render(<SessionCard s={herdr} />);
    expect(screen.getByText("Herdr")).toBeInTheDocument();
    expect(screen.getByText("w2:p1")).toBeInTheDocument();
    expect(screen.getByText(/not owned/)).toBeInTheDocument();
    expect(screen.getByText("blocked")).toBeInTheDocument();
    expect(screen.getByText("reported (id)")).toBeInTheDocument();
    expect(screen.getByText("herdr agent attach at-1234abcd-a1")).toBeInTheDocument();
    expect(screen.getByText(/Herdr reports lifecycle state, not tool calls/)).toBeInTheDocument();
  });

  it("explains a local fallback and offers no attach command", () => {
    render(
      <SessionCard
        s={{
          ...herdr,
          runtime: "local",
          fallback_to: "demo",
          failure_category: "unavailable",
          attach: { command: null, guidance: "No Herdr session." },
          capabilities: [],
        }}
      />,
    );
    expect(screen.getByText("Local fallback")).toBeInTheDocument();
    expect(screen.getByText(/ran with the demo adapter/)).toBeInTheDocument();
    expect(screen.queryByText("Open in Herdr")).not.toBeInTheDocument();
  });
});
