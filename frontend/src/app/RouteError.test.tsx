import { render, screen } from "@testing-library/react";
import { createMemoryRouter, Outlet, RouterProvider } from "react-router";
import { describe, expect, it, vi } from "vitest";

import { RouteError } from "./RouteError";

function Boom(): never {
  throw new Error("render exploded");
}

describe("RouteError", () => {
  it("keeps the layout and offers recovery when a page throws", () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    const router = createMemoryRouter(
      [
        {
          path: "/",
          element: (
            <div>
              <nav aria-label="Main">nav</nav>
              <Outlet />
            </div>
          ),
          children: [
            { errorElement: <RouteError />, children: [{ path: "boom", element: <Boom /> }] },
          ],
        },
      ],
      { initialEntries: ["/boom"] },
    );
    render(<RouterProvider router={router} />);
    expect(screen.getByRole("navigation", { name: "Main" })).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("render exploded");
    expect(screen.getByRole("button", { name: "Reload page" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Go to Overview" })).toHaveAttribute("href", "/");
  });
});
