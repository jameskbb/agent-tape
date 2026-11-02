import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Field } from "./parts";

describe("Field", () => {
  it("labels a single control without an explicit id and describes it with the hint", () => {
    render(
      <Field label="Most runs waiting at once" hint="0 means never wait.">
        <input type="number" />
      </Field>,
    );
    const input = screen.getByLabelText("Most runs waiting at once");
    expect(input).toHaveAccessibleDescription("0 means never wait.");
  });

  it("labels selects too", () => {
    render(
      <Field label="Workflow">
        <select>
          <option>One</option>
        </select>
      </Field>,
    );
    expect(screen.getByRole("combobox", { name: "Workflow" })).toBeInTheDocument();
  });

  it("labels a composite control as a group", () => {
    render(
      <Field label="Repeat every">
        <div>
          <input aria-label="Interval amount" />
        </div>
      </Field>,
    );
    expect(screen.getByRole("group", { name: "Repeat every" })).toBeInTheDocument();
  });

  it("keeps an explicit htmlFor", () => {
    render(
      <Field label="Name" htmlFor="n">
        <input id="n" />
      </Field>,
    );
    expect(screen.getByLabelText("Name")).toHaveAttribute("id", "n");
  });
});
