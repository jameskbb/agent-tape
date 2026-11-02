import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { makeDetail } from "@/test/fixtures";

import { RunOutputCard } from "./runDetailTabs";

describe("RunOutputCard", () => {
  it("shows the run's structured outputs", () => {
    const detail = {
      ...makeDetail({ finished_at: "2026-01-01T00:00:00Z" }),
      outputs: { files_changed: 3, summary: "Validated names" },
    };
    render(<RunOutputCard detail={detail} />);
    expect(screen.getByText(/"files_changed"/)).toBeInTheDocument();
    expect(screen.getByText(/"Validated names"/)).toBeInTheDocument();
  });

  it("explains when a finished run reported nothing", () => {
    render(<RunOutputCard detail={makeDetail({ finished_at: "2026-01-01T00:00:00Z" })} />);
    expect(screen.getByText(/did not report structured output/)).toBeInTheDocument();
  });
});
