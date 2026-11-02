/**
 * Reported cost, interpreted exactly like the backend budget logic
 * (`limits/budget.py::reported_cost_usd`): only US-dollar amounts an adapter reported
 * count; simulated cost is never shown as real; anything else is "unavailable".
 */
export type CostReading =
  | { kind: "usd"; amount: number; estimated: boolean; source: string | null }
  | { kind: "simulated" }
  | { kind: "unavailable"; reason: string };

export function readCost(cost: Record<string, unknown> | null | undefined): CostReading {
  if (!cost) return { kind: "unavailable", reason: "Not reported" };
  if (cost.simulated) return { kind: "simulated" };
  const source = typeof cost.source === "string" ? cost.source : null;
  const estimated =
    cost.estimated === true || (source?.toLowerCase().includes("estimate") ?? false);
  for (const key of ["usd", "total_usd", "cost_usd"]) {
    const value = cost[key];
    if (typeof value === "number" && Number.isFinite(value))
      return { kind: "usd", amount: value, estimated, source };
  }
  const amount = cost.amount;
  if (
    typeof amount === "number" &&
    Number.isFinite(amount) &&
    typeof cost.currency === "string" &&
    cost.currency.toUpperCase() === "USD"
  ) {
    return { kind: "usd", amount, estimated, source };
  }
  return { kind: "unavailable", reason: "Reported in a form other than US dollars" };
}

/** $0.0123 for small amounts, $1.23 above a dollar. */
export function formatUsd(amount: number): string {
  const digits = Math.abs(amount) < 1 ? 4 : 2;
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(amount);
}

export function sameCost(
  a: Record<string, unknown> | null,
  b: Record<string, unknown> | null,
): boolean {
  const ra = readCost(a);
  const rb = readCost(b);
  if (ra.kind === "usd" && rb.kind === "usd") return ra.amount === rb.amount;
  return ra.kind === rb.kind;
}
