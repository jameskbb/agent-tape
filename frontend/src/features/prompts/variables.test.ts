import { describe, expect, it } from "vitest";

import type { VariableDefinition } from "./api";
import {
  bodyTokens,
  buildInputs,
  cleanVariables,
  coerceDefault,
  definitionIssues,
  initialForm,
} from "./variables";

describe("bodyTokens", () => {
  it("finds variables and secret references in first-use order", () => {
    expect(bodyTokens("Fix {{ a }} then {{b}} and {{ secret:TOKEN }} and {{ a }}")).toEqual({
      variables: ["a", "b"],
      secrets: ["TOKEN"],
    });
  });
});

describe("coerceDefault", () => {
  it("coerces by type and treats empty as no default", () => {
    expect(coerceDefault("number", "12.5")).toBe(12.5);
    expect(coerceDefault("number", "abc")).toBe("abc");
    expect(coerceDefault("boolean", "true")).toBe(true);
    expect(coerceDefault("boolean", false)).toBe(false);
    expect(coerceDefault("string", "")).toBeNull();
  });
});

describe("cleanVariables", () => {
  it("keeps only fields that apply to each type", () => {
    const defs: VariableDefinition[] = [
      {
        name: " n ",
        type: "number",
        default: "3",
        options: ["x"],
        validation: { minimum: 1, pattern: "x", max_length: 4 },
        help: " ",
      },
      { name: "e", type: "enum", options: [" a ", ""], default: "a" },
    ];
    expect(cleanVariables(defs)).toEqual([
      { name: "n", type: "number", required: true, default: 3, validation: { minimum: 1 } },
      { name: "e", type: "enum", required: true, default: "a", options: ["a"] },
    ]);
  });
});

describe("definitionIssues", () => {
  it("reports undeclared tokens, duplicates, bad names, and enums without options", () => {
    const issues = definitionIssues("{{ a }} {{ missing }}", [
      { name: "a", type: "string" },
      { name: "a", type: "string" },
      { name: "1bad", type: "string" },
      { name: "choice", type: "enum", options: [] },
      { name: "tok", type: "secret", default: "not a name!" },
    ]);
    const text = issues.map((i) => `${i.variable}: ${i.message}`);
    expect(text).toEqual(
      expect.arrayContaining([
        "a: is declared twice",
        "missing: is used in the template but not declared",
        "choice: needs at least one option",
        expect.stringMatching(/^1bad: Names use/),
        expect.stringMatching(/^tok: default must be a secret reference name/),
      ]),
    );
  });

  it("is empty for a valid template", () => {
    expect(definitionIssues("Hi {{ who }}", [{ name: "who", type: "string" }])).toEqual([]);
  });
});

describe("buildInputs", () => {
  const defs: VariableDefinition[] = [
    { name: "count", type: "number" },
    { name: "flag", type: "boolean", default: true },
    { name: "note", type: "multiline" },
    { name: "who", type: "string", default: "bob" },
    { name: "tok", type: "secret" },
  ];

  it("starts from defaults", () => {
    expect(initialForm(defs)).toEqual({ count: "", flag: true, note: "", who: "bob", tok: "" });
  });

  it("omits empty values so server defaults apply, and sends numbers as numbers", () => {
    expect(buildInputs(defs, { count: "4", flag: false, note: "", who: "", tok: "GH" })).toEqual({
      count: 4,
      flag: false,
      tok: "GH",
    });
  });

  it("keeps unparseable numbers as text for the server to explain", () => {
    expect(buildInputs(defs, { count: "four" })).toEqual({ count: "four" });
  });
});
