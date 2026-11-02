import { describe, expect, it } from "vitest";

import { eventContentOf, numberedLines, parseUnifiedDiff } from "./eventContent";

describe("numberedLines", () => {
  it("parses the cat -n numbers", () => {
    const text = "     5\tdef greet(name: str) -> str:\n     6\t    return name";
    expect(numberedLines(text)).toEqual([
      [5, "def greet(name: str) -> str:"],
      [6, "    return name"],
    ]);
  });

  it("parses the U+2192 separator some Claude Code versions emit", () => {
    const text = "     1→first line";
    expect(numberedLines(text)).toEqual([[1, "first line"]]);
  });

  it("numbers plain text from 1 when no line is cat -n shaped", () => {
    expect(numberedLines("first\nsecond\nthird")).toEqual([
      [1, "first"],
      [2, "second"],
      [3, "third"],
    ]);
  });
});

describe("eventContentOf", () => {
  it("picks payload[evidence.event.content_key] when present", () => {
    const event = { payload: { output: "grep output", diff: "unused" } };
    const evidence = { event: { content_key: "output" } };
    expect(eventContentOf(event, evidence)).toEqual({ key: "output", content: "grep output" });
  });

  it("falls back to the first of output, diff, text, content, body, message", () => {
    const event = { payload: { text: "assistant said this" } };
    expect(eventContentOf(event, {})).toEqual({ key: "text", content: "assistant said this" });
  });

  it("returns null content when the event has none of the known keys", () => {
    const event = { payload: { something_else: "irrelevant" } };
    expect(eventContentOf(event, {})).toEqual({ key: null, content: null });
  });
});

describe("parseUnifiedDiff", () => {
  it("builds hunks with add/del/ctx lines and old/new numbers from a unified diff", () => {
    const text = [
      "--- a/src/greeter.py",
      "+++ b/src/greeter.py",
      "@@ -4,2 +4,3 @@ def greet(name):",
      " def greet(name):",
      "-    return name",
      "+    cleaned = name.strip()",
      "+    return cleaned",
    ].join("\n");
    const file = parseUnifiedDiff("src/greeter.py", text);
    expect(file).not.toBeNull();
    expect(file?.path).toBe("src/greeter.py");
    expect(file?.change_type).toBe("modified");
    expect(file?.diff_available).toBe(true);
    expect(file?.hunks).toHaveLength(1);
    const hunk = file?.hunks[0];
    expect(hunk?.section).toBe("def greet(name):");
    expect(hunk?.lines).toEqual([
      { kind: "ctx", text: "def greet(name):", old: 4, new: 4 },
      { kind: "del", text: "    return name", old: 5, new: null },
      { kind: "add", text: "    cleaned = name.strip()", old: null, new: 5 },
      { kind: "add", text: "    return cleaned", old: null, new: 6 },
    ]);
    expect(file?.insertions).toBe(2);
    expect(file?.deletions).toBe(1);
  });

  it("returns null for non-diff text", () => {
    expect(parseUnifiedDiff("src/greeter.py", "2 checks passed")).toBeNull();
  });
});
