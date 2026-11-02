import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ChangedFile } from "./api";
import { DiffViewer } from "./DiffViewer";

function file(overrides: Partial<ChangedFile>): ChangedFile {
  return {
    id: overrides.path ?? "f",
    path: "src/app.py",
    old_path: null,
    change_type: "modified",
    insertions: 1,
    deletions: 1,
    is_binary: false,
    size_bytes: 20,
    is_protected: false,
    protected_patterns: [],
    pre_existing_dirty: false,
    attempt_id: "att",
    hunks: [
      {
        header: "@@ -1 +1 @@",
        section: "",
        lines: [
          { kind: "del", text: 'print("old")', old: 1, new: null },
          { kind: "add", text: 'print("new")', old: null, new: 1 },
        ],
      },
    ],
    diff_available: true,
    diff_truncated: false,
    ...overrides,
  };
}

describe("DiffViewer", () => {
  it("says why an untracked file has no diff", () => {
    const missing = { change_type: "added" as const, hunks: [], diff_available: false };
    render(
      <DiffViewer
        files={[
          file({ ...missing, id: "c", path: "new/c.txt", diff_omitted: "count_cap" }),
          file({ ...missing, id: "s", path: "new/s.txt", diff_omitted: "size_cap" }),
          file({ ...missing, id: "u", path: "new/u.txt" }),
        ]}
      />,
    );
    expect(
      screen.getByText(/the run created more untracked files than AgentTape diffs/),
    ).toBeInTheDocument();
    expect(screen.getByText(/the file is over the size cap\. Check it/)).toBeInTheDocument();
    expect(
      screen.getByText(/over the size cap or recorded by the adapter without a diff/),
    ).toBeInTheDocument();
  });

  it("shows hunks with line numbers, markers, and highlighted tokens", () => {
    const { container } = render(<DiffViewer files={[file({})]} />);
    const table = screen.getByRole("table", { name: "Diff of src/app.py" });
    expect(within(table).getByText("@@ -1 +1 @@")).toBeInTheDocument();
    expect(container.querySelector('[data-line="add"]')).not.toBeNull();
    expect(container.querySelector('[data-line="del"]')).not.toBeNull();
    expect(within(table).getByText('"new"')).toHaveClass("text-[var(--st-timed-out)]");
  });

  it("collapses and expands files, and shows binary metadata only", () => {
    render(
      <DiffViewer
        files={[
          file({}),
          file({
            id: "bin",
            path: "logo.png",
            change_type: "added",
            is_binary: true,
            insertions: null,
            deletions: null,
            size_bytes: 2048,
            hunks: [],
            diff_available: false,
          }),
        ]}
      />,
    );
    const header = screen.getByRole("button", { name: /src\/app\.py/ });
    expect(header).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(header);
    expect(header).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("table", { name: "Diff of src/app.py" })).toBeNull();
    const binary = screen.getByRole("button", { name: /logo\.png/ });
    expect(binary).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(binary);
    expect(
      screen.getByText(/Binary file: content is not stored or shown\. Size 2\.0 KB/),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Expand all files" }));
    expect(header).toHaveAttribute("aria-expanded", "true");
  });

  it("flags protected paths and renames", () => {
    render(
      <DiffViewer
        files={[
          file({
            id: "p",
            path: "docs/new.md",
            old_path: "docs/old.md",
            change_type: "renamed",
            is_protected: true,
            protected_patterns: ["docs/**"],
          }),
        ]}
      />,
    );
    expect(screen.getByText("Protected path")).toBeInTheDocument();
    expect(screen.getByText("Renamed")).toBeInTheDocument();
    expect(screen.getByText("docs/old.md")).toBeInTheDocument();
  });
});
