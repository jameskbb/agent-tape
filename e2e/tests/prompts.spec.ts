import { expect, test } from "@playwright/test";

test("create a prompt with variables, save a second version, compare, and preview", async ({
  page,
}) => {
  const name = `Release checklist ${Date.now()}`;
  await page.goto("/prompts");
  await page.getByRole("button", { name: "New prompt" }).first().click();
  await expect(page).toHaveURL(/\/prompts\/new$/);

  await page
    .getByRole("textbox", { name: "Name", exact: true })
    .first()
    .fill(name);
  await page.getByRole("textbox", { name: "Owner" }).fill("Release team");
  const template = page.getByRole("textbox", { name: "Template" });
  await template.fill("Prepare {{ area }} for release on {{ day }}.");
  // "day" is used but not declared yet; the editor offers to declare it.
  await page.getByRole("button", { name: /Declare 1 from template/ }).click();
  await page.getByRole("button", { name: "Create prompt" }).click();

  await expect(page).toHaveURL(/\/prompts\/[0-9a-f-]{36}/);
  await expect(page.getByRole("heading", { level: 1 })).toContainText(name);
  await expect(page.getByText("Saving your edits creates")).toBeVisible();

  // Edit the template: saving creates version 2, version 1 is kept.
  await page
    .getByRole("textbox", { name: "Template" })
    .fill(
      "Prepare {{ area }} for release on {{ day }}.\nList the rollback steps.",
    );
  await page
    .getByRole("textbox", { name: "Version note" })
    .fill("Ask for rollback steps");
  await page.getByRole("button", { name: "Save as version 2" }).click();
  await expect(
    page.getByRole("button", { name: "Save as version 3" }),
  ).toBeVisible();

  await page.getByRole("tab", { name: "History" }).click();
  const versions = page.getByRole("region", { name: "Versions", exact: true });
  await expect(versions.getByText("v1", { exact: true })).toBeVisible();
  await expect(versions.getByText("v2", { exact: true })).toBeVisible();
  await expect(versions.getByText("Ask for rollback steps")).toBeVisible();
  const diff = page.getByRole("table", { name: "Diff from version 1 to 2" });
  await expect(
    diff.locator('[data-op="add"]', { hasText: "List the rollback steps." }),
  ).toBeVisible();

  await page.getByRole("tab", { name: "Preview" }).click();
  await expect(page.getByText("1 problem")).toBeVisible();
  await page.getByRole("textbox", { name: "day" }).fill("Friday");
  await expect(page.getByText("Ready to run")).toBeVisible();
  await expect(
    page.getByText("Prepare the API for release on Friday."),
  ).toBeVisible();
});
