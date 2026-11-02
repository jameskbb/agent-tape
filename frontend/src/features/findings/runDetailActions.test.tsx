import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement, ReactNode } from "react";
import { describe, expect, it } from "vitest";

import type { RunStatus } from "@/api/types";
import { ToastProvider } from "@/components/ui";
import { makeDetail, makeFinding, renderWithQuery } from "@/test/fixtures";

import { runDetailActions } from "./runDetailActions";

function withToast(ui: ReactNode): ReactElement {
  return <ToastProvider>{ui}</ToastProvider>;
}

const action = runDetailActions.find((a) => a.id === "challenge-findings");
if (!action) throw new Error("challenge-findings action is not registered");

const profilesPage = { items: [], total: 0, limit: 100, offset: 0, next_offset: null };

describe("runDetailActions: Challenge findings", () => {
  it("has id challenge-findings at order 5", () => {
    expect(action.id).toBe("challenge-findings");
    expect(action.order).toBe(5);
  });

  it("shows no Challenge findings button for a workflow run", () => {
    const detail = makeDetail({ id: "run-1", kind: "workflow", status: "succeeded" });
    const { container } = renderWithQuery(withToast(action.render({ detail })), {
      api: { "/runs/run-1/context/findings": [], "/agent-profiles": profilesPage },
    });
    expect(container.textContent).toBe("");
  });

  it("disables the button with the backend's own reason while the run is live", () => {
    const detail = makeDetail({ id: "run-1", kind: "agent", status: "running" as RunStatus });
    renderWithQuery(withToast(action.render({ detail })), {
      api: { "/runs/run-1/context/findings": [], "/agent-profiles": profilesPage },
    });
    const button = screen.getByRole("button", { name: "Challenge findings" });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("title", "Challenge findings needs a finished run");
  });

  it("disables the button when a finished run recorded no findings", async () => {
    const detail = makeDetail({ id: "run-1", kind: "agent", status: "succeeded" });
    renderWithQuery(withToast(action.render({ detail })), {
      api: { "/runs/run-1/context/findings": [], "/agent-profiles": profilesPage },
    });
    const button = await screen.findByRole("button", { name: "Challenge findings" });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("title", "This run recorded no findings to challenge");
  });

  it("opens the Challenge dialog on a finished run with findings", async () => {
    const user = userEvent.setup();
    const detail = makeDetail({ id: "run-1", kind: "agent", status: "succeeded" });
    renderWithQuery(withToast(action.render({ detail })), {
      api: { "/runs/run-1/context/findings": [makeFinding()], "/agent-profiles": profilesPage },
    });
    const button = screen.getByRole("button", { name: "Challenge findings" });
    await waitFor(() => expect(button).toBeEnabled());
    await user.click(button);
    expect(screen.getByRole("heading", { name: "Challenge findings" })).toBeInTheDocument();
  });

  it("renders the Challenge findings button for an observed session too", () => {
    const detail = makeDetail({ id: "run-1", kind: "observed", status: "succeeded" });
    renderWithQuery(withToast(action.render({ detail })), {
      api: { "/runs/run-1/context/findings": [], "/agent-profiles": profilesPage },
    });
    expect(screen.getByRole("button", { name: "Challenge findings" })).toBeInTheDocument();
  });
});
