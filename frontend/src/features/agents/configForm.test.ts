import { describe, expect, it } from "vitest";

import { configChanges, configToForm, fieldsFor, formToConfig } from "./configForm";

describe("profile config form mapping", () => {
  const fields = fieldsFor("claude-code");

  it("round-trips a config and preserves keys the form does not manage", () => {
    const config = {
      model: "sonnet",
      permission_mode: "plan",
      max_turns: 12,
      allowed_tools: ["Read", "Bash(git diff *)"],
      env_allowlist: ["HOME_PROXY"],
      secret_env: { GH_TOKEN: "GITHUB_TOKEN" },
      budget: { max_cost_usd: 2.5, max_total_tokens: 1000 },
      validation_commands: [["npm", "test"]],
      evaluators: [{ type: "exit_code", name: "ok", required: true, config: {} }],
      bare: true,
    };
    const form = configToForm(config, fields);
    expect(form.secret_env).toEqual([["GH_TOKEN", "GITHUB_TOKEN"]]);
    expect(form["budget.max_cost_usd"]).toBe("2.5");
    expect(formToConfig(form, fields, config)).toEqual(config);
  });

  it("drops empty values so defaults apply and keeps bad numbers for the server", () => {
    const form = configToForm({}, fields);
    form.model = "  ";
    form.max_turns = "lots";
    form.allowed_tools = ["", " Read "];
    form["budget.max_cost_usd"] = "";
    expect(formToConfig(form, fields, { budget: { max_cost_usd: 1 } })).toEqual({
      max_turns: "lots",
      allowed_tools: ["Read"],
    });
  });

  it("passes arguments exactly as written, one per row", () => {
    const generic = fieldsFor("generic-command");
    const form = configToForm({ executable: "/usr/bin/python3" }, generic);
    form.args = ["-c", "print('a b')", "", " spaced "];
    form.success_exit_codes = ["0", "2"];
    const out = formToConfig(form, generic, {});
    expect(out.args).toEqual(["-c", "print('a b')", " spaced "]);
    expect(out.success_exit_codes).toEqual([0, 2]);
    expect(out.executable).toBe("/usr/bin/python3");
  });

  it("lists key-level changes between versions", () => {
    expect(configChanges({ a: 1, b: [1] }, { b: [1, 2], c: "x" })).toEqual([
      { key: "a", before: 1, after: undefined },
      { key: "b", before: [1], after: [1, 2] },
      { key: "c", before: undefined, after: "x" },
    ]);
  });
});
