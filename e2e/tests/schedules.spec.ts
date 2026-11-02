import { expect, test } from "@playwright/test";

// The cron test creates an every-minute schedule. Pause any enabled copy after each test,
// pass or fail, so it cannot keep creating runs while later specs execute.
test.afterEach(async ({ request }) => {
  const res = await request.get("/api/v1/schedules", { params: { q: "Every minute check", limit: 100 } });
  if (!res.ok()) return;
  const body = (await res.json()) as { items: { id: string; name: string; enabled: boolean }[] };
  for (const s of body.items) {
    if (s.name === "Every minute check" && s.enabled) {
      await request.post(`/api/v1/schedules/${s.id}/pause`);
    }
  }
});

test("run a seeded schedule now and watch its run succeed", async ({ page }) => {
  await page.goto("/schedules");
  const table = page.getByRole("table", { name: "Schedules" });
  const row = table.getByRole("row", { name: /Nightly greeting service check/ });
  await expect(row.getByText("Paused").first()).toBeVisible();
  await row.getByRole("link", { name: "Nightly greeting service check" }).click();

  await expect(page.getByRole("heading", { name: "Nightly greeting service check" })).toBeVisible();
  await expect(page.getByText("Paused. Resume the schedule to see upcoming runs.")).toBeVisible();
  await page.getByRole("button", { name: "Run now" }).click();
  const confirm = page.getByRole("dialog", { name: "Run this schedule now?" });
  await confirm.getByRole("button", { name: "Run now" }).click();

  await expect(page).toHaveURL(/\/runs\/[0-9a-f-]{36}/);
  const timeline = page.getByRole("list", { name: "Run timeline" });
  await expect(timeline.getByText(/Created by schedule 'Nightly greeting service check' \(run now\)/)).toBeVisible();
  await expect(timeline.getByText("Run succeeded")).toBeVisible({ timeout: 60_000 });

  await page.goBack();
  const history = page.getByRole("table", { name: "Schedule history" });
  await expect(history.getByText("Run created")).toBeVisible();
});

test("create a cron schedule with a live preview and let the scheduler create its run", async ({
  page,
}) => {
  test.setTimeout(180_000);
  await page.goto("/schedules/new");
  await page.getByLabel("Prompt", { exact: true }).selectOption({ label: "Improve the greeting service (v1)" });
  await page.getByLabel("Agent profile").selectOption({ label: "Demo Agent" });

  await page.getByLabel("Time zone").fill("America/New_York");
  await page.getByRole("button", { name: "Weekdays at 09:00" }).click();
  const reel = page.getByRole("list", { name: "Next runs in America/New_York" });
  await expect(reel.getByRole("listitem")).toHaveCount(6);
  await expect(reel.getByText("09:00").first()).toBeVisible();

  // An invalid expression is explained instead of previewed.
  await page.getByLabel("Cron expression").fill("61 * * * *");
  await expect(page.getByText(/not a valid cron expression/)).toBeVisible();

  await page.getByLabel("Cron expression").fill("* * * * *");
  await expect(reel.getByRole("listitem")).toHaveCount(6);
  await expect(page.getByText("Wait for the previous run")).toBeVisible();
  await expect(page.getByText("Run once to catch up")).toBeVisible();
  await page.getByLabel("Name", { exact: true }).fill("Every minute check");
  await page.getByRole("button", { name: "Create schedule" }).click();

  await expect(page.getByRole("heading", { name: "Every minute check" })).toBeVisible();
  await expect(page.getByText("Enabled", { exact: true })).toBeVisible();
  await expect(page.getByText("Scheduler running")).toBeVisible();

  // The scheduler creates a normal run when the next minute arrives.
  const history = page.getByRole("table", { name: "Schedule history" });
  await expect(history.getByText("Run created").first()).toBeVisible({ timeout: 120_000 });
  await history.getByRole("link").first().click();
  await expect(page).toHaveURL(/\/runs\/[0-9a-f-]{36}/);
  await expect(
    page.getByRole("list", { name: "Run timeline" }).getByText(/Created by schedule 'Every minute check'/),
  ).toBeVisible();

  // Pause it again so it stops creating runs for the rest of the suite.
  await page.goBack();
  await page.getByRole("button", { name: "Pause" }).click();
  await expect(page.getByRole("button", { name: "Resume" })).toBeVisible();
});

test("a webhook trigger shows its token once and logs deliveries", async ({ page, request }) => {
  await page.goto("/settings");
  await page.getByRole("link", { name: /Webhook triggers/ }).click();
  await page.getByRole("button", { name: "New trigger" }).first().click();
  await page.getByLabel("Prompt", { exact: true }).selectOption({ label: "Improve the greeting service" });
  await page.getByLabel("Agent profile").selectOption({ label: "Demo Agent" });
  await page.getByLabel("JSON path 1").fill("$.client_payload.goal");
  await page.getByLabel("Variable 1").selectOption("goal");
  await page.getByRole("button", { name: "Create trigger" }).click();

  await expect(page.getByText("Copy this token now")).toBeVisible();
  const token = (await page.locator("pre").first().textContent())?.trim() ?? "";
  expect(token).toMatch(/^atw_/);
  const path = new URL((await page.locator("pre").nth(1).textContent()) ?? "").pathname;

  const bad = await request.post(path, { data: { client_payload: { goal: "x" } } });
  expect(bad.status()).toBe(401);
  const ok = await request.post(path, {
    data: { client_payload: { goal: "Add a docstring" } },
    headers: { Authorization: `Bearer ${token}`, "Idempotency-Key": "e2e-1" },
  });
  expect(ok.status()).toBe(202);
  const again = await request.post(path, {
    data: { client_payload: { goal: "Add a docstring" } },
    headers: { Authorization: `Bearer ${token}`, "Idempotency-Key": "e2e-1" },
  });
  expect(again.status()).toBe(200);

  await page.getByRole("button", { name: "I've stored the token" }).click();
  const log = page.getByRole("table", { name: "Deliveries" });
  await expect(log.getByText("accepted")).toBeVisible();
  await expect(log.getByText("duplicate")).toBeVisible();
  await expect(page.getByText("Copy this token now")).toHaveCount(0);
});
