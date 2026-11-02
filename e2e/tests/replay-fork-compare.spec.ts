import { expect, test, type Page } from "@playwright/test";

const BASELINE = "Improve the greeting service (baseline)";
const FORK = "Improve the greeting service (fork: stricter goal)";

async function openRun(page: Page, title: string) {
  await page.goto("/runs");
  await page.getByRole("table", { name: "Runs" }).getByText(title, { exact: true }).click();
  await expect(page.getByRole("heading", { name: title })).toBeVisible();
}

test("replay a run with the same versions and inputs", async ({ page }) => {
  await openRun(page, BASELINE);
  const originalUrl = page.url();
  await page.getByRole("button", { name: "Replay" }).click();
  const dialog = page.getByRole("dialog", { name: "Replay this run?" });
  await expect(dialog.getByText(/exactly the same prompt version/)).toBeVisible();
  await dialog.getByRole("button", { name: "Start replay" }).click();

  await expect(page).not.toHaveURL(originalUrl);
  await expect(page.getByRole("heading", { name: `Replay of ${BASELINE}` })).toBeVisible();
  const timeline = page.getByRole("list", { name: "Run timeline" });
  await expect(timeline.getByText("Run succeeded")).toBeVisible({ timeout: 60_000 });

  await page.getByRole("tab", { name: /Lineage/ }).click();
  await expect(page.getByRole("heading", { name: "Replay of" })).toBeVisible();
  await expect(page.getByRole("link", { name: BASELINE })).toBeVisible();
});

test("fork a run with a changed variable, then see the link both ways", async ({ page }) => {
  await openRun(page, BASELINE);
  await page.getByRole("button", { name: "Fork", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Fork this run" });
  await expect(dialog.getByText("No changes yet")).toBeVisible();
  await dialog.getByRole("textbox", { name: "Title" }).fill("Fork from e2e");
  await dialog.getByRole("checkbox", { name: /Add tests/ }).uncheck();
  await expect(dialog.getByText("1 change")).toBeVisible();
  await dialog.getByRole("button", { name: "Start fork" }).click();

  await expect(page.getByRole("heading", { name: "Fork from e2e" })).toBeVisible();
  await expect(
    page.getByRole("list", { name: "Run timeline" }).getByText("Run succeeded"),
  ).toBeVisible({ timeout: 60_000 });
  await page.getByRole("tab", { name: /Lineage/ }).click();
  await expect(page.getByRole("heading", { name: "Forked from" })).toBeVisible();
  await expect(page.getByText("inputs.add_tests")).toBeVisible();

  await page.getByRole("link", { name: BASELINE }).click();
  await page.getByRole("tab", { name: /Lineage/ }).click();
  await expect(page.getByRole("link", { name: "Fork from e2e" })).toBeVisible();
});

test("compare two runs side by side with a shareable URL", async ({ page }) => {
  await openRun(page, FORK);
  await page.getByRole("button", { name: "Compare" }).click();
  await expect(page).toHaveURL(/\/runs\/compare\?a=[0-9a-f-]{36}&b=[0-9a-f-]{36}/);
  const url = page.url();

  await page.goto(url);
  await expect(page.getByRole("heading", { name: "Compare runs" })).toBeVisible();
  await expect(page.getByTestId("relation")).toHaveText("B is a fork of A");
  const config = page.getByRole("table", { name: "Configuration comparison" });
  await expect(config.getByText("Variable: goal")).toBeVisible();
  await expect(config.locator("tr[data-changed]").first()).toBeVisible();
  const outcome = page.getByRole("table", { name: "Outcome comparison" });
  await expect(outcome.getByText("Simulated").first()).toBeVisible();
  await expect(outcome.getByText("Not reported").first()).toBeVisible();
  await expect(page.getByRole("table", { name: "Event type counts" })).toBeVisible();
  await expect(
    page.getByRole("table", { name: "Git change comparison" }).getByText("src/greeter.py"),
  ).toBeVisible();

  await page.getByRole("button", { name: "Swap" }).click();
  await expect(page.getByTestId("relation")).toHaveText("A is a fork of B");
});

test("retention settings preview a cleanup before deleting anything", async ({ page }) => {
  await page.goto("/settings");
  await page.getByRole("link", { name: /^Data retention/ }).click();
  await expect(page.getByRole("heading", { name: "Data retention" })).toBeVisible();
  await page.getByRole("button", { name: "Preview cleanup" }).click();
  await expect(page.getByTestId("retention-summary")).toHaveText(
    /Nothing would be deleted with these settings/,
  );
  await page.getByRole("button", { name: "Save settings" }).click();
  await expect(page.getByText("Retention settings saved")).toBeVisible();
});
