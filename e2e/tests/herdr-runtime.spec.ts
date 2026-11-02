import { expect, test } from "@playwright/test";

// The E2E server runs a fake Herdr (backend/tests/fixtures/fake_herdr.py), never a real one.
// Restart-and-reattach is covered by backend integration tests (test_herdr_runtime.py): it
// needs the server process restarted mid-run, which Playwright's single webServer cannot do.

test("Runtime settings show Herdr status, capabilities, and a working connection test", async ({
  page,
}) => {
  await page.goto("/settings");
  await page.getByRole("link", { name: /Runtime/ }).click();
  await expect(page).toHaveURL(/\/settings\/runtime$/);
  await expect(page.getByText("Reachable", { exact: true })).toBeVisible();
  await expect(page.getByText("0.9.0-fake (API protocol 22)")).toBeVisible();
  await expect(
    page.getByRole("table", { name: "Runtime capabilities" }),
  ).toBeVisible();
  await expect(
    page.getByText("A new AgentTape-owned workspace per attempt"),
  ).toBeVisible();

  await page.getByRole("button", { name: "Test connection" }).click();
  const results = page.getByRole("list", { name: "Connection test results" });
  await expect(results.getByText("Server answers ping")).toBeVisible();
  await expect(results.getByText("Event subscription")).toBeVisible();
});

test("a Herdr demo run: create the profile, watch lifecycle, approve, and attach guidance", async ({
  page,
  request,
}) => {
  // Choose the Herdr runtime in the existing Agents UI.
  await page.goto("/agents/new?adapter=herdr");
  await page
    .getByRole("textbox", { name: "Name", exact: true })
    .fill("Herdr demo agent (e2e)");
  await expect(page.getByLabel("Agent", { exact: true })).toHaveValue("demo");
  await page.getByRole("button", { name: "Save profile" }).click();
  await expect(page).toHaveURL(/\/agents\/[0-9a-f-]{36}$/);
  const profileId = page.url().split("/").pop() ?? "";

  // The demo agent works in the seeded evidence project (the AgentTape fixture repository
  // with Git isolation), exactly like the local "Demo Agent (full evidence)" profile.
  const projects = (await (
    await request.get("/api/v1/projects?limit=100")
  ).json()) as {
    items: { id: string; name: string }[];
  };
  const project = projects.items.find(
    (p) => p.name === "AgentTape Evidence Demo",
  );
  expect(project, "seeded evidence project").toBeTruthy();

  // Runs start from the API, CLI, schedules, triggers, or workflows; there is no generic
  // "run this profile" button in the UI.
  const created = await request.post("/api/v1/runs", {
    data: {
      agent_profile_id: profileId,
      project_id: project!.id,
      prompt: "Run the evidence demo in Herdr.",
    },
  });
  expect(created.ok()).toBeTruthy();
  const runId = ((await created.json()) as { run: { id: string } }).run.id;

  await page.goto(`/runs/${runId}`);
  const timeline = page.getByRole("list", { name: "Run timeline" });
  await expect(
    timeline.getByText(/Created AgentTape-owned Herdr workspace/),
  ).toBeVisible({
    timeout: 60_000,
  });
  await expect(
    timeline.getByText(/Started the demo agent \(start\)/),
  ).toBeVisible();

  // The demo agent asks for approval; Herdr shows the pane blocked meanwhile.
  const approve = page.getByRole("button", { name: "Approve" });
  await expect(approve).toBeVisible({ timeout: 60_000 });
  await page.getByRole("tab", { name: /Runtime/ }).click();
  const sessions = page.getByLabel("Runtime sessions");
  await expect(sessions.getByText("Herdr", { exact: true })).toBeVisible();
  await expect(sessions.getByText("blocked", { exact: true })).toBeVisible();
  await expect(sessions.getByText(/created by AgentTape/)).toBeVisible();
  await expect(
    sessions.getByText(/^herdr agent attach w\d+:p\d+$/),
  ).toBeVisible();

  await approve.click();
  const confirm = page.getByRole("dialog", { name: "Approve this request?" });
  await confirm.getByRole("textbox").fill("Protected write is expected");
  await confirm.getByRole("button", { name: "Approve" }).click();
  await page.getByRole("tab", { name: /Timeline/ }).click();
  await expect(timeline.getByText("Run succeeded")).toBeVisible({
    timeout: 60_000,
  });
  await expect(
    timeline.getByText(/Started the demo agent \(resume\)/),
  ).toBeVisible();

  await page.getByRole("tab", { name: /Runtime/ }).click();
  await expect(sessions.getByText("idle", { exact: true })).toBeVisible();

  // The same file evidence as a local run: the README edit was restored and the protected
  // CI workflow was created after the approval.
  const files = (await (
    await request.get(`/api/v1/runs/${runId}/files?limit=200`)
  ).json()) as {
    items: { file_key: string; classification: string }[];
  };
  const byPath = new Map(
    files.items.map((f) => [f.file_key, f.classification]),
  );
  expect(byPath.get("README.md")).toBe("restored");
  expect(byPath.get(".github/workflows/ci.yml")).toBe("created");
});
