import { expect, test } from "@playwright/test";

test("onboarding: project, Demo Agent, seeded prompt, live run to success", async ({
  page,
}) => {
  await page.goto("/");
  const banner = page.getByRole("complementary", { name: "Set up AgentTape" });
  await expect(banner).toBeVisible();
  await banner.getByRole("link", { name: "Start setup" }).click();
  await expect(page).toHaveURL(/\/welcome$/);

  await page.getByRole("button", { name: "Get started" }).click();

  // Step 1: the demo project is offered and preselected.
  await expect(
    page.getByRole("heading", { name: "Choose a project" }),
  ).toBeVisible();
  await expect(
    page.getByRole("radio", { name: /^AgentTape Demo Project Recommended/ }),
  ).toBeChecked();
  await page.getByRole("button", { name: "Continue" }).click();

  // Step 2: the Demo Agent.
  await expect(
    page.getByRole("heading", { name: "Choose an agent" }),
  ).toBeVisible();
  await page.getByRole("radio", { name: /^Demo Agent Recommended/ }).check();
  await page.getByRole("button", { name: "Continue" }).click();

  // Step 3: a seeded prompt with variables, previewed live.
  await page
    .getByRole("radio", { name: /Improve the greeting service/ })
    .check();
  await page
    .getByRole("textbox", { name: "goal" })
    .fill("Reject empty names with a clear error message.");
  await expect(page.getByText("Ready to run")).toBeVisible();
  await expect(
    page.getByText(/Goal: Reject empty names with a clear error message\./),
  ).toBeVisible();
  await page.getByRole("button", { name: "Continue" }).click();

  // Step 4: start the run and land on its live timeline.
  await page.getByRole("button", { name: "Start the run" }).click();
  await expect(page).toHaveURL(/\/runs\/[0-9a-f-]{36}/);
  const timeline = page.getByRole("list", { name: "Run timeline" });
  await expect(timeline.getByText("Run queued")).toBeVisible();
  await expect(timeline.getByText("Run succeeded")).toBeVisible({
    timeout: 60_000,
  });

  // Completing setup hides the banner.
  await page.goto("/");
  await expect(
    page.getByRole("complementary", { name: "Set up AgentTape" }),
  ).toHaveCount(0);
});
