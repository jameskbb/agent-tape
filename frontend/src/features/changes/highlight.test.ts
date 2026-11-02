import { describe, expect, it } from "vitest";

import { languageFor, tokenize } from "./highlight";

describe("languageFor", () => {
  it("maps extensions and special names", () => {
    expect(languageFor("src/greeter.py")).toBe("py");
    expect(languageFor("web/App.tsx")).toBe("js");
    expect(languageFor(".github/workflows/ci.yml")).toBe("yaml");
    expect(languageFor("Makefile")).toBe("sh");
    expect(languageFor("LICENSE")).toBeNull();
    expect(languageFor("data.bin")).toBeNull();
  });
});

describe("tokenize", () => {
  it("colours keywords, strings, numbers, and comments in Python", () => {
    const tokens = tokenize('    return f"Hello, {name}!"  # greet 2', "py");
    expect(tokens.find((t) => t.kind === "keyword")?.text).toBe("return");
    expect(tokens.find((t) => t.kind === "string")?.text).toBe('"Hello, {name}!"');
    expect(tokens.at(-1)).toEqual({ kind: "comment", text: "# greet 2" });
    expect(tokens.map((t) => t.text).join("")).toBe('    return f"Hello, {name}!"  # greet 2');
  });

  it("keeps an unterminated string on one line", () => {
    const tokens = tokenize("const s = 'open", "js");
    expect(tokens.at(-1)).toEqual({ kind: "string", text: "'open" });
  });

  it("recognises YAML keys", () => {
    const tokens = tokenize("  runs-on: ubuntu-latest", "yaml");
    expect(tokens.find((t) => t.kind === "key")?.text).toBe("runs-on");
  });

  it("returns plain text for unknown languages", () => {
    expect(tokenize("anything # here", null)).toEqual([{ kind: "plain", text: "anything # here" }]);
  });

  it("colours numbers", () => {
    expect(tokenize("x = 42", "py").find((t) => t.kind === "number")?.text).toBe("42");
  });
});
