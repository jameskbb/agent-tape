import { expect, test } from "@playwright/test";

/**
 * Traces a finding from the Context tab back to the prompt, a repository source, a
 * knowledge source, and a live verification call, and checks the stale / unavailable / gap
 * reports and the pending correction.
 */
const DEMO = "Refactor with Full File Evidence";

test("trace a finding in the evidence demo run back to its sources", async ({ page, request }) => {
  const runs = (await (await request.get("/api/v1/runs?limit=200")).json()) as {
    items: { id: string; title: string; status: string }[];
  };
  const run = runs.items.find((r) => r.title === DEMO && r.status === "succeeded");
  expect(run, "seeded evidence demo run").toBeTruthy();
  await page.goto(`/runs/${run!.id}?tab=context`);

  // Prompt and pack.
  await expect(page.getByRole("heading", { name: "Context summary" })).toBeVisible();
  await expect(page.getByText("Prompt template and version")).toBeVisible();
  await expect(page.getByRole("heading", { name: /Context pack v\d+/ })).toBeVisible();

  // The structured finding cites at least two sources, one snapshot and one live.
  const finding = page.getByTestId("finding").first();
  await expect(finding).toBeVisible();
  const supports = finding.locator("button");
  expect(await supports.count()).toBeGreaterThanOrEqual(2);
  await expect(finding.getByText("snapshot").first()).toBeVisible();
  await expect(finding.getByText("live").first()).toBeVisible();

  // Open one source and see its access history and the link back to the finding.
  await supports.first().click();
  const drawer = page.getByRole("dialog");
  await expect(drawer.getByText(/Access history/)).toBeVisible();
  await expect(drawer.getByText("Supports").first()).toBeVisible();
  await drawer.getByRole("button", { name: "Close source details" }).click();

  // The fake MCP call is recorded with sanitized args, a row count, and a hash.
  await page.getByRole("combobox", { name: "Filter by stage" }).selectOption("retrieved");
  const explorer = page.getByRole("table", { name: "Context evidence records" });
  await expect(explorer.getByText(/rows, hash/).first()).toBeVisible();

  // Compact index searched before targeted retrieval; stale, unavailable, and gap reported.
  await expect(page.getByRole("heading", { name: "Freshness warnings" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Context gaps and unavailable tools" })).toBeVisible();

  // The simulated correction is pending review, not applied.
  const correction = page.getByTestId("feedback-item").filter({ hasText: /correction/i }).first();
  await expect(correction.getByText("Pending", { exact: true })).toBeVisible();
});
