import { describe, expect, it } from "vitest";

import { formatUsd, readCost, sameCost } from "./cost";

describe("readCost", () => {
  it("is unavailable when nothing was reported", () => {
    expect(readCost(null)).toEqual({ kind: "unavailable", reason: "Not reported" });
  });
  it("reads the Claude Code shape and marks it an estimate", () => {
    expect(
      readCost({
        total_usd: 0.0421,
        currency: "USD",
        source: "claude-code total_cost_usd (client-side estimate)",
      }),
    ).toMatchObject({
      kind: "usd",
      amount: 0.0421,
      estimated: true,
    });
  });
  it("accepts amount + USD and rejects other currencies", () => {
    expect(readCost({ amount: 2, currency: "usd" })).toMatchObject({
      kind: "usd",
      amount: 2,
      estimated: false,
    });
    expect(readCost({ amount: 2, currency: "EUR" }).kind).toBe("unavailable");
  });
  it("never treats simulated cost as real", () => {
    expect(readCost({ usd: 1, simulated: true })).toEqual({ kind: "simulated" });
  });
  it("formats and compares", () => {
    expect(formatUsd(0.0421)).toBe("$0.0421");
    expect(formatUsd(12.5)).toBe("$12.50");
    expect(sameCost({ usd: 1 }, { total_usd: 1, source: "x" })).toBe(true);
    expect(sameCost(null, { usd: 1 })).toBe(false);
  });
});
