import { expect, test } from "@playwright/test";

/**
 * Proves the run timeline is delivered by the Server-Sent Events stream, not by polling:
 * after the first page of events, every REST events request returns an empty page, so the
 * only way later events can reach the timeline is the /events/stream connection.
 */
test("live timeline events arrive over Server-Sent Events", async ({ page, request }) => {
  const created = (await (
    await request.post("/api/v1/demo/runs", { data: { scenario: "success" } })
  ).json()) as { run: { id: string } };
  const runId = created.run.id;

  let restPages = 0;
  let firstPageEvents = -1;
  await page.route(`**/api/v1/runs/${runId}/events?*`, async (route) => {
    restPages += 1;
    if (restPages === 1) {
      const response = await route.fetch();
      firstPageEvents = ((await response.json()) as { items: unknown[] }).items.length;
      await route.fulfill({ response });
      return;
    }
    await route.fulfill({
      json: { items: [], next_after_sequence: null, last_sequence: 0, total: 0 },
    });
  });
  const streamOpened = page.waitForRequest((r) => r.url().includes(`/runs/${runId}/events/stream`));

  await page.goto(`/runs/${runId}`);
  await streamOpened;
  await expect(page.getByText("Streaming live")).toBeVisible();

  const timeline = page.getByRole("list", { name: "Run timeline" });
  // These events are written after the first REST page, so they can only come via SSE.
  await expect(timeline.getByText("Artifact saved: summary.md")).toBeVisible({ timeout: 60_000 });
  // Once the page sees the run finish it stops the stream and re-syncs the tail over REST
  // (blocked here), so the final lifecycle event may or may not arrive by SSE. Everything up
  // to the artifacts must have.
  expect(firstPageEvents).toBeGreaterThanOrEqual(0);
  const shown = await timeline.locator("li[data-sequence]").count();
  expect(shown).toBeGreaterThan(firstPageEvents + 5);
  const sequences = await timeline
    .locator("li[data-sequence]")
    .evaluateAll((items) => items.map((li) => Number(li.getAttribute("data-sequence"))));
  // No gaps and no duplicates in what the stream delivered.
  expect(sequences).toEqual(Array.from({ length: sequences.length }, (_, i) => i + 1));
});
