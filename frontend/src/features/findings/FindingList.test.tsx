import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { makeFinding } from "@/test/fixtures";

import { FindingList } from "./FindingList";

describe("FindingList", () => {
  it("wraps a claim at word boundaries and still breaks a long unbroken path", () => {
    // `break-all` split ordinary words mid-word ("whitespa|ce"). `overflow-wrap: anywhere`
    // wraps prose at spaces and only breaks inside a token too long for any line (a path or
    // a hash), and unlike `break-words` it also shrinks the min-content size, so the list's
    // grid column never grows to fit an unbroken path.
    const longPath = `${"nested/".repeat(30)}long-filename-for-visual-truncation-check.py`;
    const finding = makeFinding({ text: `Moved the helper to ${longPath}` });
    render(
      <FindingList
        findings={[finding]}
        selectedKey={finding.key}
        onSelect={() => {}}
        live={false}
      />,
    );
    const claim = screen.getByText(`Moved the helper to ${longPath}`);
    expect(claim.className).toContain("[overflow-wrap:anywhere]");
    expect(claim.className).not.toContain("break-all");
  });
});
