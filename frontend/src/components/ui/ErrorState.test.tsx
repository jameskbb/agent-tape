import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";

import { ApiError } from "@/api/client";

import { ErrorState } from "./ErrorState";

describe("ErrorState", () => {
  it("renders a not-found state with a way back and no retry for 404s", () => {
    render(
      <MemoryRouter>
        <ErrorState
          error={new ApiError(404, "not_found", "schedule 'x' was not found")}
          resource="schedule"
          backTo="/schedules"
          backLabel="Back to schedules"
          onRetry={() => undefined}
        />
      </MemoryRouter>,
    );
    expect(screen.getByText("This schedule does not exist")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to schedules" })).toHaveAttribute(
      "href",
      "/schedules",
    );
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull();
  });

  it("offers a retry for other failures", () => {
    render(
      <MemoryRouter>
        <ErrorState error={new ApiError(500, "internal_error", "boom")} onRetry={() => undefined} />
      </MemoryRouter>,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("boom");
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });
});
