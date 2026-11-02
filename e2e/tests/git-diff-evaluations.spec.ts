import { expect, test } from "@playwright/test";

const BASELINE = "Improve the greeting service (baseline)";

async function openBaseline(page: import("@playwright/test").Page) {
  await page.goto("/runs");
  await page.getByRole("table", { name: "Runs" }).getByText(BASELINE, { exact: true }).click();
  await expect(page.getByRole("heading", { name: BASELINE })).toBeVisible();
}

test("the Git diff view shows the starting revision, diff, protected paths, and validations", async ({
  page,
}) => {
  await openBaseline(page);
  await page.getByRole("tab", { name: /Changes/ }).click();

  await expect(page.getByText("Starting revision")).toBeVisible();
  await expect(page.getByText("Isolated worktree")).toBeVisible();
  await expect(page.getByText(/agenttape\/run-/).first()).toBeVisible();
  await expect(page.getByText("1 protected path was changed")).toBeVisible();

  const files = page.getByRole("table", { name: "Changed files" });
  await expect(files.getByText("src/greeter.py")).toBeVisible();
  await expect(files.getByText("tests/test_greeter.py")).toBeVisible();
  await expect(files.getByText("Protected path")).toBeVisible();

  const diff = page.getByRole("table", { name: "Diff of src/greeter.py" });
  await expect(diff.getByText("cleaned = name.strip()")).toBeVisible();
  await expect(diff.locator('[data-line="del"]').first()).toBeVisible();
  // Files collapse and expand.
  await page.getByRole("button", { name: /src\/greeter\.py/ }).click();
  await expect(diff).toHaveCount(0);
  await page.getByRole("button", { name: /src\/greeter\.py/ }).click();
  await expect(page.getByRole("table", { name: "Diff of src/greeter.py" })).toBeVisible();

  await expect(page.getByText("python3 -m compileall -q src tests")).toBeVisible();
  await expect(page.getByText("git diff --check")).toBeVisible();

  await page.getByRole("tab", { name: /Evaluations/ }).click();
  await expect(page.getByText("Exit code is 0")).toBeVisible();
  await expect(page.getByText(/Validation: python3 -m compileall/)).toBeVisible();
});

test("a commit happens only after approval, then the clean worktree can be removed", async ({
  page,
}) => {
  await openBaseline(page);
  await page.getByRole("tab", { name: /Changes/ }).click();
  const message = page.getByRole("textbox", { name: "Commit message" });
  await message.fill("Validate names in greet()\n\nApproved from the e2e test.");
  await page.getByRole("button", { name: "Request commit" }).click();
  await expect(page.getByText("Commit waiting for approval")).toBeVisible();

  await page.getByRole("button", { name: "Approve" }).click();
  const dialog = page.getByRole("dialog", { name: "Approve this request?" });
  await expect(dialog.getByText(/one local commit/)).toBeVisible();
  await dialog.getByRole("button", { name: "Approve" }).click();

  await expect(page.getByText("Validate names in greet()", { exact: true })).toBeVisible();
  await expect(page.getByText("Clean", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "Remove worktree" }).click();
  await page
    .getByRole("dialog", { name: "Remove this worktree?" })
    .getByRole("button", { name: "Remove worktree" })
    .click();
  await expect(page.getByText(/^Removed/)).toBeVisible();
});
