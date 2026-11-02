import { readFile } from "node:fs/promises";

import { expect, test, type APIRequestContext } from "@playwright/test";

const PROJECT = "AgentTape Evidence Demo";
const PROFILE = "Demo Agent (full evidence)";
const PROMPT = "Improve the greeting service";
const SEEDED_RUN = "Refactor with Full File Evidence";

async function idOf(
  request: APIRequestContext,
  path: string,
  name: string,
): Promise<string> {
  const res = await request.get(`/api/v1${path}`, {
    params: { q: name, limit: 100 },
  });
  expect(res.ok(), `GET ${path}`).toBeTruthy();
  const body = (await res.json()) as { items: { id: string; name: string }[] };
  const item = body.items.find((i) => i.name === name);
  expect(item, `${name} in ${path}`).toBeTruthy();
  return item!.id;
}

test("file evidence arrives live, pauses on a protected path, and exports a sanitized bundle", async ({
  page,
}) => {
  test.setTimeout(150_000);
  // The run-creation UI has no picker for this profile, so start it through the same API.
  const [projectId, profileId, promptId] = await Promise.all([
    idOf(page.request, "/projects", PROJECT),
    idOf(page.request, "/agent-profiles", PROFILE),
    idOf(page.request, "/prompts", PROMPT),
  ]);
  const created = await page.request.post("/api/v1/runs", {
    data: {
      title: "File evidence e2e",
      project_id: projectId,
      agent_profile_id: profileId,
      prompt_template_id: promptId,
      inputs: { goal: "Refactor greet() and wire the formatter into CI." },
      source: "ui",
    },
  });
  expect(created.ok(), await created.text()).toBeTruthy();
  const { run } = (await created.json()) as { run: { id: string } };

  await page.goto(`/runs/${run.id}?tab=files`);
  await expect(page.getByRole("tab", { name: /Files/ })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  const ledger = page.getByRole("table", { name: "File ledger" });

  // Evidence arrives while the run is still live (before the protected-path approval).
  await expect(
    ledger.getByRole("button", { name: "src/greeter.py" }),
  ).toBeVisible({
    timeout: 60_000,
  });
  await expect(page.getByText(/so far; updating live/)).toBeVisible();

  // The run pauses on the protected-path approval for the CI workflow.
  const approve = page.getByRole("button", { name: "Approve", exact: true });
  await expect(approve).toBeVisible({ timeout: 60_000 });
  await expect(
    page.locator('[data-status="waiting_for_approval"]').first(),
  ).toBeVisible();

  // README.md was edited and then put back.
  const readme = ledger
    .getByRole("row")
    .filter({ has: page.getByRole("button", { name: "README.md" }) });
  await expect(readme.getByText("Restored", { exact: true })).toBeVisible();
  await expect(readme.getByText("Touched, net unchanged")).toBeVisible();
  // A plain in-project file says it is inside the boundary, never "none".
  await expect(readme.getByText("Inside boundary", { exact: true })).toBeVisible();

  await approve.click();
  const confirm = page.getByRole("dialog", { name: "Approve this request?" });
  await confirm.getByRole("button", { name: "Approve" }).click();
  await expect(page.locator('[data-status="succeeded"]').first()).toBeVisible({
    timeout: 60_000,
  });

  // After the final reconciliation: the protected finding is approved, noise is collapsed.
  await expect(page.getByText(/protected-path finding/)).toBeVisible();
  await expect(
    ledger.getByRole("button", { name: ".github/workflows/ci.yml" }),
  ).toBeVisible();
  await expect(
    page.getByText("approved", { exact: true }).first(),
  ).toBeVisible();
  await expect(ledger.getByText("node_modules/")).toBeVisible();
  await expect(
    ledger.getByText(/filesystem events collapsed by policy/).first(),
  ).toBeVisible();

  // Filters hit the server: search narrows the ledger to one file.
  await page.getByRole("searchbox", { name: "Search paths" }).fill("greeter");
  await expect(ledger.getByRole("button", { name: "README.md" })).toHaveCount(
    0,
  );
  await expect(
    ledger.getByRole("button", { name: "src/greeter.py" }),
  ).toBeVisible();

  // The drawer shows the evidence trail and the final diff.
  await ledger.getByRole("button", { name: "src/greeter.py" }).click();
  const drawer = page.getByRole("dialog", {
    name: "File evidence: src/greeter.py",
  });
  await expect(drawer.getByText("Why AgentTape lists this file")).toBeVisible();
  const trail = drawer.getByRole("table", { name: "Evidence event trail" });
  await expect(trail.getByText("Read", { exact: true }).first()).toBeVisible();
  await expect(
    trail.getByText("Modify", { exact: true }).first(),
  ).toBeVisible();
  await expect(
    drawer.getByRole("table", { name: "Diff of src/greeter.py" }),
  ).toBeVisible();
  await expect(drawer.locator('[data-line="add"]').first()).toBeVisible();
  await drawer.getByRole("button", { name: "Close" }).click();
  await expect(drawer).toBeHidden();
  await page.getByRole("searchbox", { name: "Search paths" }).fill("");

  // The sanitized evidence bundle downloads with the API's auth handling.
  const downloading = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export evidence bundle" }).click();
  const download = await downloading;
  const bundle = JSON.parse(
    await readFile((await download.path())!, "utf8"),
  ) as {
    format: string;
    evidence_events: unknown[];
  };
  expect(bundle.format).toBe("agenttape.evidence-bundle");
  expect(bundle.evidence_events.length).toBeGreaterThan(0);
  const raw = JSON.stringify(bundle);
  expect(raw).not.toContain("/tmp/");
  expect(raw).not.toContain("/home/");
});

test("the seeded evidence run and the project File Activity page", async ({
  page,
}) => {
  await page.goto("/runs");
  await page
    .getByRole("table", { name: "Runs" })
    .getByText(SEEDED_RUN, { exact: true })
    .click();
  await expect(page.getByRole("heading", { name: SEEDED_RUN })).toBeVisible();
  await page.getByRole("tab", { name: /Files/ }).click();
  await expect(page.getByText(/Writes and final changes/)).toBeVisible();
  const ledger = page.getByRole("table", { name: "File ledger" });
  await expect(
    ledger.getByRole("button", { name: "src/greeter.py" }),
  ).toBeVisible();

  // Group by directory, then only protected paths.
  await page
    .getByRole("combobox", { name: "Group by" })
    .selectOption("directory");
  await expect(
    page.getByRole("table", { name: "File ledger: src/", exact: true }),
  ).toBeVisible();
  await page.getByRole("combobox", { name: "Group by" }).selectOption("none");
  await page.getByRole("checkbox", { name: "Protected paths" }).check();
  await expect(
    ledger.getByRole("button", { name: ".github/workflows/ci.yml" }),
  ).toBeVisible();
  await expect(
    ledger.getByRole("button", { name: "src/greeter.py" }),
  ).toHaveCount(0);

  // Cross-run activity for the project, and one file's run history.
  await page.getByRole("link", { name: "Project file activity" }).click();
  await expect(
    page.getByRole("heading", { name: "File activity", level: 1 }),
  ).toBeVisible();
  const mostTouched = page.getByRole("table", { name: "Most touched files" });
  await mostTouched.getByRole("button", { name: "src/greeter.py" }).click();
  const history = page.getByRole("table", {
    name: "Run history of src/greeter.py",
  });
  await expect(
    history.getByRole("link", { name: SEEDED_RUN, exact: true }),
  ).toBeVisible();
  await expect(
    page
      .getByRole("table", { name: "Protected-path activity" })
      .getByText(".github/workflows/ci.yml")
      .first(),
  ).toBeVisible();

  // Reachable from the navigation too.
  await page
    .getByRole("navigation", { name: "Main" })
    .getByRole("link", { name: "File Activity" })
    .click();
  await expect(page.getByRole("link", { name: PROJECT })).toBeVisible();
});
