import { expect, test } from "@playwright/test";

interface Step {
  node_key: string;
  status: string;
  attempt_count: number;
  child_run_id: string | null;
}

test("a workflow runs in parallel, pauses at approval, and resumes without redoing nodes", async ({
  page,
  request,
}) => {
  await page.goto("/workflows");
  await page.getByRole("link", { name: "Safe Code Change", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Safe Code Change" })).toBeVisible();
  // The accessible list view describes the same graph.
  await page.getByRole("tab", { name: /List/ }).click();
  await expect(
    page.getByRole("list", { name: /dependency order/ }).getByText("List the tracked sources"),
  ).toBeVisible();

  await page.getByRole("button", { name: /Start run of v1/ }).click();
  await expect(page).toHaveURL(/\/runs\/[0-9a-f-]{36}\?tab=workflow/);
  const runId = /\/runs\/([0-9a-f-]{36})/.exec(page.url())?.[1] ?? "";

  // The run pauses at the approval node; the graph shows each node's live status.
  const approveNode = page.locator('[data-node-key="approve"]');
  await expect(approveNode).toHaveAttribute("data-status", "waiting_for_approval", {
    timeout: 90_000,
  });
  await expect(page.locator('[data-node-key="implement"]')).toHaveAttribute(
    "data-status",
    "succeeded",
  );
  await expect(page.locator('[data-node-key="baseline"]')).toHaveAttribute(
    "data-status",
    "succeeded",
  );
  await expect(page.locator('[data-node-key="validate"]')).toHaveAttribute("data-status", "queued");

  const before = (await (await request.get(`/api/v1/runs/${runId}`)).json()) as {
    steps: Step[];
    children: { id: string }[];
  };
  const childBefore = before.steps.find((s) => s.node_key === "implement")?.child_run_id;

  // Decide in the approval inbox.
  await page.getByRole("link", { name: "Approvals" }).first().click();
  const card = page
    .getByRole("listitem")
    .filter({ has: page.locator(`a[href="/runs/${runId}"]`) });
  await expect(card).toBeVisible();
  await card.getByRole("button", { name: "Approve" }).click();
  const dialog = page.getByRole("dialog", { name: "Approve this request?" });
  await dialog.getByRole("textbox").fill("Ship it");
  await dialog.getByRole("button", { name: "Approve" }).click();
  await expect(page.getByText("Approved", { exact: true }).first()).toBeVisible();

  await page.goto(`/runs/${runId}?tab=workflow`);
  await expect(page.locator('[data-node-key="collect"]')).toHaveAttribute(
    "data-status",
    "succeeded",
    { timeout: 90_000 },
  );
  await expect(page.locator('[data-node-key="evaluate"]')).toHaveAttribute(
    "data-status",
    "succeeded",
  );

  // Completed nodes were not rerun: one attempt each and the same single child run.
  const after = (await (await request.get(`/api/v1/runs/${runId}`)).json()) as {
    run: { status: string };
    steps: Step[];
    children: { id: string }[];
  };
  expect(after.run.status).toBe("succeeded");
  expect(after.steps.every((s) => s.status === "succeeded" && s.attempt_count === 1)).toBe(true);
  expect(after.children).toHaveLength(1);
  expect(after.steps.find((s) => s.node_key === "implement")?.child_run_id).toBe(childBefore);

  await page.getByRole("tab", { name: /Evaluations/ }).click();
  await expect(page.getByText("Small diff, no protected paths").first()).toBeVisible();
  await expect(page.getByText(/simulated/).first()).toBeVisible();
});
