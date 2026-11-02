import { expect, test, type Page } from "@playwright/test";

async function startDemo(page: Page, profile: string) {
  await page.getByRole("button", { name: "Start a demo run" }).first().click();
  const dialog = page.getByRole("dialog", { name: "Start a Demo Agent run" });
  await dialog.getByText(profile, { exact: true }).click();
  await dialog.getByRole("button", { name: "Start run" }).click();
  await expect(page).toHaveURL(/\/runs\/[0-9a-f-]{36}/);
}

test("start a demo run and watch it succeed live", async ({ page }) => {
  // Seeds already contain finished runs (the Git compare pair), so start from the runs list.
  await page.goto("/runs");
  await startDemo(page, "Demo Agent");

  const timeline = page.getByRole("list", { name: "Run timeline" });
  await expect(timeline.getByText("Run queued")).toBeVisible();
  // Events stream in while the run is live (the recording marker is present).
  await expect(page.getByText("Recording")).toBeVisible();
  await expect(timeline.getByText("Run succeeded")).toBeVisible({ timeout: 60_000 });
  await expect(page.locator('[data-status="succeeded"]').first()).toBeVisible();
  await expect(page.getByText("Simulated", { exact: true })).toBeVisible();

  await page.getByRole("tab", { name: /Changes/ }).click();
  await expect(page.getByRole("table", { name: "Changed files" }).getByText("src/greeter.py")).toBeVisible();
  await page.getByRole("tab", { name: /Evaluations/ }).click();
  await expect(page.getByText("Exit code is 0")).toBeVisible();
  await page.getByRole("tab", { name: /Timeline/ }).click();

  await page.getByRole("button", { name: /Tools/ }).click();
  await expect(timeline.getByText("read_file(src/greeter.py)")).toBeVisible();
  await expect(timeline.getByText("Run succeeded")).toHaveCount(0);

  await page.getByRole("link", { name: "Runs" }).first().click();
  await expect(
    page.getByRole("table", { name: "Runs" }).getByText("Improve the greeting service").first(),
  ).toBeVisible();
});

test("an approval pauses the run and approving resumes it without redoing work", async ({ page }) => {
  await page.goto("/runs");
  await startDemo(page, "Demo Agent (needs approval)");
  const approve = page.getByRole("button", { name: "Approve" });
  await expect(approve).toBeVisible({ timeout: 60_000 });
  await expect(page.locator('[data-status="waiting_for_approval"]').first()).toBeVisible();
  await approve.click();
  const confirm = page.getByRole("dialog", { name: "Approve this request?" });
  await confirm.getByRole("textbox").fill("Looks safe");
  await confirm.getByRole("button", { name: "Approve" }).click();

  const timeline = page.getByRole("list", { name: "Run timeline" });
  await expect(timeline.getByText("Run succeeded")).toBeVisible({ timeout: 60_000 });
  await expect(timeline.getByText(/Approval approved/)).toBeVisible();
  await expect(timeline.locator('[data-event-type="reasoning_summary"]')).toHaveCount(1);
});
