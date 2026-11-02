import { expect, test, type APIRequestContext } from "@playwright/test";

/**
 * Context evidence with the built-in Demo Agent: the Context tab shows the context pack and
 * the provenance trail, a correction stays pending on its run until a person approves it,
 * the approved lesson is injected into the next run, and the comparison page explains how
 * the two runs' context differed.
 */

const PROMPT = "Tidy src/greeter.py without changing its behaviour.";

async function ids(
  request: APIRequestContext,
): Promise<{ repoPath: string; profile: string }> {
  const projects = (await (
    await request.get("/api/v1/projects?limit=100")
  ).json()) as {
    items: { id: string; name: string; repo_path: string }[];
  };
  const profiles = (await (
    await request.get("/api/v1/agent-profiles?limit=100")
  ).json()) as {
    items: { id: string; name: string }[];
  };
  const project = projects.items.find(
    (p) => p.name === "AgentTape Demo Project",
  );
  const profile = profiles.items.find((p) => p.name === "Demo Agent");
  expect(project, "seeded demo project").toBeTruthy();
  expect(profile, "seeded Demo Agent profile").toBeTruthy();
  return { repoPath: project!.repo_path, profile: profile!.id };
}

async function runToCompletion(
  request: APIRequestContext,
  title: string,
  project: string,
  profile: string,
): Promise<string> {
  const created = await request.post("/api/v1/runs", {
    data: {
      title,
      project_id: project,
      agent_profile_id: profile,
      prompt: PROMPT,
    },
  });
  expect(created.ok()).toBeTruthy();
  const id = ((await created.json()) as { run: { id: string } }).run.id;
  await expect
    .poll(
      async () =>
        (
          (await (await request.get(`/api/v1/runs/${id}`)).json()) as {
            run: { status: string };
          }
        ).run.status,
      { timeout: 60_000 },
    )
    .toBe("succeeded");
  return id;
}

test("trace context, approve a correction, and see it injected and compared", async ({
  page,
  request,
}) => {
  const ids_ = await ids(request);
  const profile = ids_.profile;
  // A project of its own (same fixture repository) so the lesson approved here is only
  // injected into this spec's runs, not into other specs' demo-project runs.
  const created = await request.post("/api/v1/projects", {
    data: { name: `Context e2e ${Date.now()}`, repo_path: ids_.repoPath },
  });
  expect(created.ok(), await created.text()).toBeTruthy();
  const project = ((await created.json()) as { id: string }).id;
  try {
    const first = await runToCompletion(
      request,
      "Context e2e: before the lesson",
      project,
      profile,
    );

    await page.goto(`/runs/${first}?tab=context`);
    await expect(
      page.getByRole("heading", { name: "Context summary" }),
    ).toBeVisible();
    await expect(
      page.getByRole("heading", { name: "Context pack v1" }),
    ).toBeVisible();
    await expect(
      page.getByText("Repository file index (git ls-files)").first(),
    ).toBeVisible();
    // The agent's reported read shows up as retrieved, never as injected or cited.
    const explorer = page.getByRole("table", {
      name: "Context evidence records",
    });
    await page
      .getByRole("combobox", { name: "Filter by stage" })
      .selectOption("retrieved");
    await expect(
      explorer.getByRole("button", { name: "src/greeter.py" }).first(),
    ).toBeVisible();
    await expect(explorer.getByText("Cited", { exact: true })).toHaveCount(0);

    // Open the source drawer from the explorer.
    await explorer
      .getByRole("button", { name: "src/greeter.py" })
      .first()
      .click();
    const drawer = page.getByRole("dialog", { name: "src/greeter.py" });
    await expect(drawer.getByText(/Access history/)).toBeVisible();
    await drawer.getByRole("button", { name: "Close source details" }).click();

    // Record a correction; it stays pending on this run.
    const lesson =
      "Greeting strings are user-facing copy; keep them unchanged.";
    await page
      .getByRole("combobox", { name: "Feedback kind" })
      .selectOption("correction");
    await page.getByRole("textbox", { name: "Feedback text" }).fill(lesson);
    await page.getByRole("button", { name: "Record for review" }).click();
    const item = page.getByTestId("feedback-item").filter({ hasText: lesson });
    await expect(item.getByText("Pending", { exact: true })).toBeVisible();

    // A person classifies it, reviews the proposed destination, and approves.
    await item
      .getByRole("combobox", { name: /Classify feedback/ })
      .selectOption("business-knowledge");
    await expect(
      item.getByRole("combobox", { name: "Proposed destination" }),
    ).toHaveValue("project_instruction");
    await item.getByRole("button", { name: "Approve lesson" }).click();
    await expect(item.getByText("Approved", { exact: true })).toBeVisible();
    await expect(item.getByText(/lesson v\d+/)).toBeVisible();

    // The next run of the project receives the approved lesson.
    const second = await runToCompletion(
      request,
      "Context e2e: after the lesson",
      project,
      profile,
    );
    await page.goto(`/runs/${second}?tab=context`);
    await expect(page.getByText(/Approved lesson v\d+/).first()).toBeVisible();
    await expect(page.getByText(lesson).first()).toBeVisible();

    // The comparison explains the difference in supplied context.
    await page.goto(`/runs/compare?a=${first}&b=${second}`);
    const context = page.getByRole("region", { name: "Context" });
    await expect(
      context.getByRole("heading", { name: "Approved lessons injected" }),
    ).toBeVisible();
    await expect(context.getByText("Only in B").first()).toBeVisible();

    // Project Context Activity lists the lesson with a link back to its originating run.
    await page.goto(`/context?project=${project}`);
    const approved = page
      .getByTestId("approved-lesson")
      .filter({ hasText: lesson });
    await expect(approved).toBeVisible();
    await expect(
      approved.getByRole("link", { name: first.slice(0, 8) }),
    ).toBeVisible();
  } finally {
    // Archive it again: a non-demo project would hide the onboarding banner other specs expect.
    await request.patch(`/api/v1/projects/${project}`, {
      data: { archived: true },
    });
  }
});
