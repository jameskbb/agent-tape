import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { RUN_STATUSES } from "@/api/types";

import { StatusPill, STATUS_STYLES } from "./StatusPill";
import { Unavailable } from "./Unavailable";

describe("StatusPill", () => {
  it.each(RUN_STATUSES)("renders a readable label for %s", (status) => {
    const { container } = render(<StatusPill status={status} />);
    expect(screen.getByText(STATUS_STYLES[status].label)).toBeInTheDocument();
    expect(container.querySelector(`[data-status="${status}"]`)).not.toBeNull();
  });

  it("has a distinct label for every status", () => {
    const labels = RUN_STATUSES.map((s) => STATUS_STYLES[s].label);
    expect(new Set(labels).size).toBe(RUN_STATUSES.length);
  });

  it("marks active statuses with the live indicator instead of an icon", () => {
    const { container, rerender } = render(<StatusPill status="running" />);
    expect(container.querySelector(".at-live-dot")).not.toBeNull();
    rerender(<StatusPill status="succeeded" />);
    expect(container.querySelector(".at-live-dot")).toBeNull();
    expect(container.querySelector("svg")).not.toBeNull();
  });
});

describe("Unavailable", () => {
  it("says a value was not reported instead of showing zero", () => {
    render(<Unavailable reason="Not reported by this adapter" />);
    const el = screen.getByLabelText("Not reported by this adapter");
    expect(el).toHaveTextContent("Not reported by this adapter");
    expect(el).not.toHaveTextContent("0");
  });

  it("has a sensible default", () => {
    render(<Unavailable />);
    expect(screen.getByText("Not reported")).toBeInTheDocument();
  });
});
