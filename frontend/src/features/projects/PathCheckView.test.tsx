import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { PathCheck } from "./api";
import { PathCheckView, pathVerdict } from "./PathCheckView";

const base: PathCheck = {
  input: "/repos/app",
  canonical: "/repos/app",
  allowed: false,
  exists: true,
  is_dir: true,
  is_git_repo: false,
  matched_root: "/repos",
  allowed_roots: ["/repos"],
  message: "",
  git: null,
};

describe("pathVerdict", () => {
  it("classifies each outcome", () => {
    expect(pathVerdict({ ...base, allowed: true })).toBe("allowed");
    expect(pathVerdict({ ...base, canonical: null })).toBe("invalid");
    expect(pathVerdict({ ...base, matched_root: null })).toBe("outside");
    expect(pathVerdict({ ...base, exists: false })).toBe("missing");
    expect(pathVerdict({ ...base, is_dir: false })).toBe("not-a-directory");
  });
});

describe("PathCheckView", () => {
  it("shows the canonical path and allowed roots when outside the allowlist", () => {
    render(
      <PathCheckView
        loading={false}
        check={{
          ...base,
          input: "/repos/../etc",
          canonical: "/etc",
          matched_root: null,
          message: "Outside the allowlisted project roots.",
        }}
      />,
    );
    expect(screen.getByText("Outside the allowlisted roots")).toBeInTheDocument();
    expect(screen.getByText("/etc")).toBeInTheDocument();
    expect(screen.getByText("/repos")).toBeInTheDocument();
  });

  it("shows Git state for an allowed repository", () => {
    render(
      <PathCheckView
        loading={false}
        check={{
          ...base,
          allowed: true,
          is_git_repo: true,
          message: "Allowed: inside /repos.",
          git: {
            is_repo: true,
            head: "0123456789abcdef",
            branch: "main",
            dirty: true,
            dirty_files: ["a.py"],
            dirty_count: 1,
            worktree_supported: true,
            error: null,
          },
        }}
      />,
    );
    expect(screen.getByText("Allowed")).toBeInTheDocument();
    expect(screen.getByText("main")).toBeInTheDocument();
    expect(screen.getByText("0123456789")).toBeInTheDocument();
    expect(screen.getByText("1 uncommitted change")).toBeInTheDocument();
  });
});
