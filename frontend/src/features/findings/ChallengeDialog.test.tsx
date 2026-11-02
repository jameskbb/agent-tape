import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter, useLocation } from "react-router";
import { describe, expect, it, vi } from "vitest";

import { ToastProvider } from "@/components/ui";
import { makeDetail, renderWithQuery } from "@/test/fixtures";

import { ChallengeDialog } from "./ChallengeDialog";

function withToast(ui: ReactElement): ReactElement {
  return <ToastProvider>{ui}</ToastProvider>;
}

const run = makeDetail({ id: "run-1", title: "Refactor with Full File Evidence" }).run;

const profilesPage = {
  items: [
    { id: "prof-1", name: "Demo Agent (reviewer)" },
    { id: "prof-2", name: "Careful Reviewer" },
  ],
  total: 2,
  limit: 100,
  offset: 0,
  next_offset: null,
};

const challengeResult = {
  run: { ...run, id: "child-1", title: "Challenge: Refactor with Full File Evidence" },
  findings_reviewed: ["F1", "F2"],
  omitted: 0,
  neutralized: [],
};

describe("ChallengeDialog", () => {
  it("shows the eyebrow, title, finding count, reviewer select, and title placeholder", () => {
    renderWithQuery(withToast(<ChallengeDialog run={run} findingCount={3} onClose={() => {}} />), {
      api: { "/agent-profiles": profilesPage },
    });
    expect(screen.getByText("Model judgment")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Challenge findings" })).toBeInTheDocument();
    expect(screen.getByText(/reads this run's 3 findings/)).toBeInTheDocument();
    expect(
      screen.getByPlaceholderText("Challenge: Refactor with Full File Evidence"),
    ).toBeInTheDocument();
  });

  it("reads a single finding as singular in the body sentence", () => {
    renderWithQuery(withToast(<ChallengeDialog run={run} findingCount={1} onClose={() => {}} />), {
      api: { "/agent-profiles": profilesPage },
    });
    expect(screen.getByText(/reads this run's 1 finding /)).toBeInTheDocument();
  });

  it("disables Start challenge until a reviewer is chosen", async () => {
    const user = userEvent.setup();
    renderWithQuery(withToast(<ChallengeDialog run={run} findingCount={2} onClose={() => {}} />), {
      api: { "/agent-profiles": profilesPage },
    });
    const submit = screen.getByRole("button", { name: "Start challenge" });
    expect(submit).toBeDisabled();
    await screen.findByRole("option", { name: "Demo Agent (reviewer)" });
    await user.selectOptions(screen.getByRole("combobox", { name: "Reviewer agent" }), "prof-1");
    expect(submit).toBeEnabled();
  });

  it("starts a challenge, toasts, and navigates to the child run", async () => {
    const user = userEvent.setup();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    let posted: unknown = null;
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url =
          typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
        if (url.includes("/agent-profiles")) {
          return Promise.resolve(
            new Response(JSON.stringify(profilesPage), {
              status: 200,
              headers: { "Content-Type": "application/json" },
            }),
          );
        }
        if (url.includes("/challenge")) {
          posted = typeof init?.body === "string" ? JSON.parse(init.body) : null;
          return Promise.resolve(
            new Response(JSON.stringify(challengeResult), {
              status: 201,
              headers: { "Content-Type": "application/json" },
            }),
          );
        }
        return Promise.resolve(
          new Response(JSON.stringify({ error: { code: "not_found", message: "Not found" } }), {
            status: 404,
            headers: { "Content-Type": "application/json" },
          }),
        );
      }),
    );
    function LocationDisplay() {
      return <span data-testid="loc">{useLocation().pathname}</span>;
    }
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={["/runs/run-1"]}>
          {withToast(<ChallengeDialog run={run} findingCount={2} onClose={() => {}} />)}
          <LocationDisplay />
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await screen.findByRole("option", { name: "Demo Agent (reviewer)" });
    await user.selectOptions(screen.getByRole("combobox", { name: "Reviewer agent" }), "prof-1");
    await user.click(screen.getByRole("button", { name: "Start challenge" }));
    expect(await screen.findByText("Challenge started")).toBeInTheDocument();
    expect(posted).toMatchObject({ agent_profile_id: "prof-1" });
    // The toast can render before the router commits the navigation, so wait for it.
    await waitFor(() => expect(screen.getByTestId("loc")).toHaveTextContent("/runs/child-1"));
    vi.unstubAllGlobals();
  });

  it("toasts the server message verbatim on a 409 and keeps the dialog open", async () => {
    const user = userEvent.setup();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL) => {
        const url =
          typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
        if (url.includes("/agent-profiles")) {
          return Promise.resolve(
            new Response(JSON.stringify(profilesPage), {
              status: 200,
              headers: { "Content-Type": "application/json" },
            }),
          );
        }
        return Promise.resolve(
          new Response(
            JSON.stringify({
              error: {
                code: "run_not_finished",
                message: "Challenge findings needs a finished run",
              },
            }),
            { status: 409, headers: { "Content-Type": "application/json" } },
          ),
        );
      }),
    );
    render(
      <QueryClientProvider client={client}>
        <MemoryRouter>
          {withToast(<ChallengeDialog run={run} findingCount={2} onClose={() => {}} />)}
        </MemoryRouter>
      </QueryClientProvider>,
    );
    await screen.findByRole("option", { name: "Demo Agent (reviewer)" });
    await user.selectOptions(screen.getByRole("combobox", { name: "Reviewer agent" }), "prof-1");
    await user.click(screen.getByRole("button", { name: "Start challenge" }));
    expect(await screen.findByText("Challenge findings needs a finished run")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Challenge findings" })).toBeInTheDocument();
    vi.unstubAllGlobals();
  });
});
