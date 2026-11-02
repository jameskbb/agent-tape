import { expect, test } from "@playwright/test";

interface RunLite {
  id: string;
  kind: string;
  status: string;
  parent_run_id: string | null;
}

test("a finished workflow run can be replayed and compared; fork explains why it does not apply", async ({ page, request }) => {
  // Start a fresh workflow run from the seeded "Safe Code Change" workflow and approve it.
  const workflows = (await (await request.get("/api/v1/workflows?q=Safe%20Code%20Change")).json()) as { items: { id: string }[] };
  const workflowId = workflows.items[0]?.id;
  expect(workflowId).toBeTruthy();
  const started = (await (
    await request.post(`/api/v1/workflows/${workflowId}/runs`, { data: { inputs: { goal: "Reject empty names." } } })
  ).json()) as { run: RunLite };
  const runId = started.run.id;
  await expect
    .poll(async () => {
      const approvals = (await (await request.get(`/api/v1/approvals?status=pending&run_id=${runId}`)).json()) as { items: { id: string }[] };
      const pending = approvals.items[0];
      if (pending) await request.post(`/api/v1/approvals/${pending.id}/approve`, { data: { note: "e2e" } });
      return ((await (await request.get(`/api/v1/runs/${runId}`)).json()) as { run: RunLite }).run.status;
    }, { timeout: 60_000, intervals: [500] })
    .toBe("succeeded");

  await page.goto(`/runs/${runId}`);
  await page.getByRole("button", { name: "Fork" }).click();
  await expect(page.getByRole("dialog", { name: "This run cannot be forked" })).toContainText("replay reruns this workflow version");
  await page.getByRole("button", { name: "Close" }).click();

  await page.getByRole("button", { name: "Replay" }).click();
  const dialog = page.getByRole("dialog", { name: "Replay this run?" });
  await expect(dialog).toContainText("same workflow version and inputs");
  await dialog.getByRole("button", { name: "Start replay" }).click();
  await expect(page).not.toHaveURL(new RegExp(`/runs/${runId}$`));
  const replayId = page.url().split("/runs/")[1]?.split("?")[0] ?? "";
  const replay = ((await (await request.get(`/api/v1/runs/${replayId}`)).json()) as { run: RunLite }).run;
  expect(replay.kind).toBe("workflow");
  expect(replay.parent_run_id).toBe(runId);

  await page.getByRole("button", { name: "Compare" }).click();
  await expect(page).toHaveURL(new RegExp(`/runs/compare\\?a=${runId}&b=${replayId}`));
  await expect(page.getByRole("heading", { name: "Compare runs" })).toBeVisible();
});
