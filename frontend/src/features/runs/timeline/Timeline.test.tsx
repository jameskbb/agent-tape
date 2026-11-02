import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { MemoryRouter, useLocation } from "react-router";
import { describe, expect, it } from "vitest";

import { makeDetail, makeEvent } from "@/test/fixtures";

import { Timeline } from "./Timeline";

const events = [
  makeEvent({ type: "lifecycle", summary: "Run queued" }),
  makeEvent({
    type: "tool_call",
    summary: "read_file(src/greeter.py)",
    payload: { tool: "read_file", input: { path: "src/greeter.py" } },
  }),
  makeEvent({
    type: "stdout",
    summary: "syntax ok",
    payload: { text: "syntax ok: src/greeter.py" },
  }),
  makeEvent({
    type: "usage",
    summary: "Simulated usage",
    payload: { input_tokens: 10, output_tokens: 5, simulated: true },
  }),
];

function rows() {
  return within(screen.getByRole("list", { name: "Run timeline" })).getAllByRole("listitem");
}

function eventAt(index: number) {
  const event = events[index];
  if (!event) throw new Error(`fixture missing event ${index}`);
  return event;
}

/** `Timeline` reads `?event=<sequence>` via `useSearchParams`, so every render needs a
 * `MemoryRouter`; `initialEntries` sets the URL a test lands on. */
function renderTimeline(ui: ReactElement, initialEntries: string[] = ["/"]) {
  return render(<MemoryRouter initialEntries={initialEntries}>{ui}</MemoryRouter>);
}

describe("Timeline", () => {
  it("renders events and filters by group chip", async () => {
    renderTimeline(<Timeline detail={makeDetail()} events={events} loading={false} live={false} />);
    expect(rows()).toHaveLength(4);
    await userEvent.click(screen.getByRole("button", { name: /Tools/ }));
    expect(rows()).toHaveLength(1);
    expect(screen.getByText("read_file(src/greeter.py)")).toBeInTheDocument();
    expect(screen.getByText("Showing 1 of 4 events")).toBeInTheDocument();
  });

  it("filters by search text and offers to clear", async () => {
    renderTimeline(<Timeline detail={makeDetail()} events={events} loading={false} live={false} />);
    await userEvent.type(
      screen.getByRole("searchbox", { name: "Search events" }),
      "nothing-matches",
    );
    expect(screen.getByText("No events match these filters")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(rows()).toHaveLength(4);
  });

  it("expands an event and shows the raw JSON inspector", async () => {
    renderTimeline(<Timeline detail={makeDetail()} events={events} loading={false} live={false} />);
    await userEvent.click(screen.getByRole("button", { name: /Simulated usage/ }));
    expect(screen.getByText("Simulated by the Demo Agent")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Show raw event" }));
    expect(screen.getByText(/"schema_version"/)).toBeInTheDocument();
  });

  it("shows a recording marker while live", () => {
    renderTimeline(
      <Timeline detail={makeDetail({ status: "running" })} events={events} loading={false} live />,
    );
    expect(screen.getByText("Recording")).toBeInTheDocument();
  });

  it("explains an empty timeline", () => {
    renderTimeline(
      <Timeline detail={makeDetail({ status: "queued" })} events={[]} loading={false} live />,
    );
    expect(screen.getByText("No events yet")).toBeInTheDocument();
  });

  it("expands the event named by ?event=, gives it an id, and turns follow off", () => {
    const target = eventAt(2);
    renderTimeline(<Timeline detail={makeDetail()} events={events} loading={false} live />, [
      `/?event=${target.sequence}`,
    ]);
    const row = document.getElementById(`event-${target.sequence}`);
    expect(row).not.toBeNull();
    if (!row) throw new Error("row missing");
    expect(row).toContainElement(screen.getByText("syntax ok: src/greeter.py"));
    const toggle = within(row).getByRole("button", { name: /syntax ok/ });
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    const followCheckbox = screen.getByRole("checkbox", { name: "Follow live" });
    expect(followCheckbox).not.toBeChecked();
  });

  it("turning Follow live on while ?event= is set leaves the linked event and follows", async () => {
    const target = eventAt(2);
    function Search() {
      return <output data-testid="search">{useLocation().search}</output>;
    }
    renderTimeline(
      <>
        <Timeline detail={makeDetail()} events={events} loading={false} live />
        <Search />
      </>,
      [`/?tab=timeline&event=${target.sequence}`],
    );
    const followCheckbox = screen.getByRole("checkbox", { name: "Follow live" });
    expect(followCheckbox).not.toBeChecked();
    expect(followCheckbox).toHaveAttribute("title", expect.stringMatching(/linked event/));
    await userEvent.click(followCheckbox);
    expect(screen.getByRole("checkbox", { name: "Follow live" })).toBeChecked();
    expect(screen.getByTestId("search")).toHaveTextContent("?tab=timeline");
    expect(screen.getByTestId("search").textContent).not.toContain("event=");
    expect(screen.getByRole("checkbox", { name: "Follow live" })).not.toHaveAttribute("title");
  });

  it("clears a filter that hides the ?event= target so it still renders", async () => {
    const toolEvent = eventAt(1);
    renderTimeline(
      <Timeline detail={makeDetail()} events={events} loading={false} live={false} />,
      [`/?event=${toolEvent.sequence}`],
    );
    // The Output chip would normally filter down to only Output events, hiding the Tools
    // event named by ?event=; since it is the target, the filter resets instead of hiding it.
    await userEvent.click(screen.getByRole("button", { name: /Output/ }));
    expect(rows()).toHaveLength(4);
    expect(document.getElementById(`event-${toolEvent.sequence}`)).not.toBeNull();
  });

  it("keeps today's behaviour when there is no ?event= param", () => {
    renderTimeline(<Timeline detail={makeDetail()} events={events} loading={false} live={false} />);
    expect(rows()).toHaveLength(4);
    for (const event of events) {
      expect(document.getElementById(`event-${event.sequence}`)).not.toBeNull();
    }
  });
});
