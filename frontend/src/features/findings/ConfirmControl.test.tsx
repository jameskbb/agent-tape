import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";

import { ToastProvider } from "@/components/ui";

import { makeFinding } from "@/test/fixtures";

import type { Finding } from "./api";
import { ConfirmControl } from "./ConfirmControl";

function withToast(ui: ReactElement): ReactElement {
  return <ToastProvider>{ui}</ToastProvider>;
}

function renderControl(
  finding: Finding,
  fetchImpl: (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>,
) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  vi.stubGlobal("fetch", vi.fn(fetchImpl));
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>{withToast(<ConfirmControl finding={finding} runId="run-1" />)}</MemoryRouter>
    </QueryClientProvider>,
  );
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function urlOf(input: RequestInfo | URL): string {
  return typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
}

/** The dialog's own submit button, scoped inside the `<dialog>` so it is never confused with
 * the trigger button behind it (both can carry the same accessible name). */
function dialogSubmit(name: string): HTMLElement {
  return within(screen.getByRole("dialog")).getByRole("button", { name });
}

describe("ConfirmControl", () => {
  it("shows 'Confirm finding' for an unconfirmed finding and opens the confirm dialog", async () => {
    const user = userEvent.setup();
    renderControl(makeFinding({ id: "f-1", key: "F1" }), () =>
      Promise.resolve(jsonResponse({ error: { code: "not_found", message: "Not found" } }, 404)),
    );
    await user.click(screen.getByRole("button", { name: "Confirm finding" }));
    expect(screen.getByRole("heading", { name: "Confirm finding" })).toBeInTheDocument();
    expect(
      screen.getByText(/You are recording that you reviewed this finding/),
    ).toBeInTheDocument();
  });

  it("shows 'Withdraw confirmation' for a confirmed finding and explains what withdrawing does", async () => {
    const user = userEvent.setup();
    const finding = makeFinding({
      id: "f-1",
      key: "F1",
      confirmation: { actor: "James", ts: "2026-01-01T00:00:00Z", note: null },
    });
    renderControl(finding, () =>
      Promise.resolve(jsonResponse({ error: { code: "not_found", message: "Not found" } }, 404)),
    );
    const button = screen.getByRole("button", { name: "Withdraw confirmation" });
    await user.click(button);
    expect(screen.getByRole("heading", { name: "Withdraw confirmation" })).toBeInTheDocument();
    expect(screen.getByText(/The confirmation is withdrawn/)).toBeInTheDocument();
  });

  it("never makes withdrawing a danger action: neither the trigger nor the dialog's submit", async () => {
    const user = userEvent.setup();
    const finding = makeFinding({
      id: "f-1",
      key: "F1",
      confirmation: { actor: "James", ts: "2026-01-01T00:00:00Z", note: null },
    });
    renderControl(finding, () =>
      Promise.resolve(jsonResponse({ error: { code: "not_found", message: "Not found" } }, 404)),
    );
    const trigger = screen.getByRole("button", { name: "Withdraw confirmation" });
    expect(trigger).toHaveAttribute("data-variant", "secondary");
    await user.click(trigger);
    expect(dialogSubmit("Withdraw confirmation")).toHaveAttribute("data-variant", "primary");
  });

  it("confirms a finding, toasts 'Finding confirmed', and closes the dialog", async () => {
    const user = userEvent.setup();
    let posted: unknown = null;
    renderControl(makeFinding({ id: "f-1", key: "F1" }), (input, init) => {
      const url = urlOf(input);
      if (url.includes("/confirm")) {
        posted = typeof init?.body === "string" ? JSON.parse(init.body) : null;
        return Promise.resolve(
          jsonResponse({
            finding_id: "f-1",
            confirmation: { actor: "James", ts: "2026-01-01T00:00:00Z", note: "looks right" },
          }),
        );
      }
      return Promise.resolve(jsonResponse({ error: { code: "not_found", message: "n" } }, 404));
    });
    await user.click(screen.getByRole("button", { name: "Confirm finding" }));
    await user.type(screen.getByLabelText(/Note/), "looks right");
    await user.click(dialogSubmit("Confirm finding"));
    expect(await screen.findByText("Finding confirmed")).toBeInTheDocument();
    expect(posted).toMatchObject({ note: "looks right" });
    expect(screen.queryByRole("heading", { name: "Confirm finding" })).not.toBeInTheDocument();
  });

  it("withdraws a confirmation and toasts 'Confirmation withdrawn'", async () => {
    const user = userEvent.setup();
    const finding = makeFinding({
      id: "f-1",
      key: "F1",
      confirmation: { actor: "James", ts: "2026-01-01T00:00:00Z", note: null },
    });
    renderControl(finding, (input) => {
      const url = urlOf(input);
      if (url.includes("/confirm/withdraw")) {
        return Promise.resolve(jsonResponse({ finding_id: "f-1", confirmation: null }));
      }
      return Promise.resolve(jsonResponse({ error: { code: "not_found", message: "n" } }, 404));
    });
    await user.click(screen.getByRole("button", { name: "Withdraw confirmation" }));
    await user.click(dialogSubmit("Withdraw confirmation"));
    expect(await screen.findByText("Confirmation withdrawn")).toBeInTheDocument();
  });

  it("keeps the dialog open with an inline error on a 409, and shows no success toast", async () => {
    const user = userEvent.setup();
    renderControl(makeFinding({ id: "f-1", key: "F1" }), (input) => {
      const url = urlOf(input);
      if (url.includes("/confirm")) {
        return Promise.resolve(
          jsonResponse(
            { error: { code: "conflict", message: "This finding is already confirmed" } },
            409,
          ),
        );
      }
      return Promise.resolve(jsonResponse({ error: { code: "not_found", message: "n" } }, 404));
    });
    await user.click(screen.getByRole("button", { name: "Confirm finding" }));
    await user.click(dialogSubmit("Confirm finding"));
    expect(await screen.findByRole("alert")).toHaveTextContent("This finding is already confirmed");
    expect(screen.getByRole("heading", { name: "Confirm finding" })).toBeInTheDocument();
    expect(screen.queryByText("Finding confirmed")).not.toBeInTheDocument();
  });
});
