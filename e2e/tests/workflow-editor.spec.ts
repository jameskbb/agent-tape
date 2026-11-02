import { expect, test } from "@playwright/test";

/**
 * The workflow editor after creation: rename and archive without a new version, node retry
 * and evaluator fields, a confirm before removing a node, and a guard against leaving with
 * unsaved changes.
 */
test("edit an existing workflow's details, guard unsaved changes, and confirm node removal", async ({
  page,
  request,
}) => {
  const suffix = Date.now().toString(36);
  const res = await request.post("/api/v1/workflows", {
    data: {
      name: `Editor check ${suffix}`,
      definition: {
        nodes: [
          { key: "draft", type: "transform", name: "Draft", config: { template: "hello" } },
          { key: "finish", type: "transform", name: "Finish", config: { template: "done" } },
        ],
        edges: [{ from: "draft", to: "finish" }],
      },
    },
  });
  expect(res.ok(), await res.text()).toBe(true);
  const workflow = (await res.json()) as { id: string };

  await page.goto(`/workflows/${workflow.id}/edit`);
  await expect(page.getByRole("heading", { name: `Edit Editor check ${suffix}` })).toBeVisible();

  // Node fields the engine reads are editable.
  await expect(page.getByLabel("Retry backoff (seconds)")).toBeVisible();

  // Removing a node asks first; keeping it leaves the graph alone.
  await page.getByRole("button", { name: "Remove node" }).click();
  const removeDialog = page.getByRole("dialog", { name: "Remove node draft?" });
  await expect(removeDialog).toContainText("Its 1 connection will be removed too");
  await removeDialog.getByRole("button", { name: "Keep as is" }).click();
  const nodes = page.getByRole("list", { name: "Workflow nodes (select one to edit)" });
  await expect(nodes.getByRole("button", { name: /Draft draft/ })).toBeVisible();

  // Rename and archive save immediately, without a new version.
  await page.getByRole("tab", { name: "Workflow" }).click();
  await page.getByLabel("Name", { exact: true }).fill(`Editor renamed ${suffix}`);
  await page.getByRole("checkbox", { name: /Archived/ }).check();
  await page.getByRole("button", { name: "Save details" }).click();
  await expect(page.getByText("Workflow archived")).toBeVisible();
  await expect(page.getByRole("heading", { name: `Edit Editor renamed ${suffix}` })).toBeVisible();
  const saved = (await (await request.get(`/api/v1/workflows/${workflow.id}`)).json()) as {
    name: string;
    archived: boolean;
    latest_version: number;
  };
  expect(saved).toMatchObject({ name: `Editor renamed ${suffix}`, archived: true, latest_version: 1 });

  // An unsaved graph change blocks in-app navigation until the user chooses to leave.
  await page.getByRole("tab", { name: "Node" }).click();
  await page.getByRole("button", { name: /Add after/ }).click();
  await page.getByRole("link", { name: `Editor renamed ${suffix}` }).click();
  const leave = page.getByRole("dialog", { name: "Leave without saving?" });
  await expect(leave).toBeVisible();
  await leave.getByRole("button", { name: "Keep as is" }).click();
  await expect(page).toHaveURL(new RegExp(`/workflows/${workflow.id}/edit$`));
  await page.getByRole("link", { name: `Editor renamed ${suffix}` }).click();
  await leave.getByRole("button", { name: "Leave without saving" }).click();
  await expect(page).toHaveURL(new RegExp(`/workflows/${workflow.id}$`));
});

/**
 * A finding_review node can be added after an agent run node, targets it, picks a
 * reviewer agent, saves without validation errors, and shows up in the workflow's graph.
 */
test("add a finding_review node after an agent run node and see it in the graph", async ({
  page,
  request,
}) => {
  const suffix = Date.now().toString(36);
  const profiles = (await (await request.get("/api/v1/agent-profiles?limit=100")).json()) as {
    items: { id: string; name: string }[];
  };
  const demoAgent = profiles.items.find((p) => p.name === "Demo Agent");
  expect(demoAgent, "seeded Demo Agent profile").toBeTruthy();

  const res = await request.post("/api/v1/workflows", {
    data: {
      name: `Finding review check ${suffix}`,
      definition: {
        nodes: [
          {
            key: "agent",
            type: "agent_run",
            name: "Agent",
            config: { agent_profile_id: demoAgent!.id, prompt: "Say hello." },
          },
        ],
      },
    },
  });
  expect(res.ok(), await res.text()).toBe(true);
  const workflow = (await res.json()) as { id: string };

  await page.goto(`/workflows/${workflow.id}/edit`);
  await expect(
    page.getByRole("heading", { name: `Edit Finding review check ${suffix}` }),
  ).toBeVisible();

  await page
    .getByLabel("Node type")
    .selectOption({ label: "Finding review (challenge findings)" });
  await page.getByRole("button", { name: "Add after agent" }).click();

  await page.getByLabel("Target node").selectOption({ label: "Agent" });
  await page.getByLabel("Reviewer agent").selectOption({ label: "Demo Agent" });

  await page.getByRole("button", { name: "Save new version" }).click();
  await expect(page.getByText("Saved version 2")).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`/workflows/${workflow.id}$`));
  await expect(page.locator('[data-node-key="review"]')).toContainText("Finding review");
});
