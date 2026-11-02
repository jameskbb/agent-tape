import AxeBuilder from "@axe-core/playwright";
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

/**
 * Accessibility checks over every page a user can reach: nav pages, list and detail pages,
 * creation forms, editors, and settings sub-pages. axe runs with the WCAG 2.0/2.1 A and AA
 * rule sets in light and dark themes at desktop width and at tablet width (768px), and
 * fails on serious or critical violations. React Flow's attribution panel is excluded (the
 * graph is described by the step list beside it).
 *
 * The Findings views are covered too: the seeded evidence run's Findings tab with a stale
 * citation selected (F2) and a quote-not-found citation selected (F4), a finished Demo
 * Agent run's empty Findings tab, the cross-run Findings inbox (/findings), and the
 * evidence run's Context tab (its findings card).
 */

async function firstId(request: APIRequestContext, path: string): Promise<string> {
  const body = (await (await request.get(`/api/v1${path}`)).json()) as { items: { id: string }[] };
  const id = body.items[0]?.id;
  expect(id, `no item from ${path}`).toBeTruthy();
  return id!;
}

async function allPages(request: APIRequestContext): Promise<string[]> {
  const runs = ((await (await request.get("/api/v1/runs?limit=100")).json()) as {
    items: { id: string; kind: string; status: string; parent_run_id: string | null }[];
  }).items;
  const agentRun = runs.find((r) => r.kind === "agent" && r.status === "succeeded");
  const workflowRun = runs.find((r) => r.kind === "workflow");
  const fork = runs.find((r) => r.parent_run_id);
  const evidenceRun = (
    (await (
      await request.get(
        `/api/v1/runs?q=${encodeURIComponent("Refactor with Full File Evidence")}&limit=50`,
      )
    ).json()) as { items: { id: string; title: string }[] }
  ).items.find((r) => r.title === "Refactor with Full File Evidence");
  expect(evidenceRun, "seeded primary evidence run").toBeTruthy();
  let triggers = (await (await request.get("/api/v1/triggers")).json()) as { items: { id: string }[] };
  if (triggers.items.length === 0) {
    const prompt = await firstId(request, "/prompts");
    const profile = await firstId(request, "/agent-profiles");
    await request.post("/api/v1/triggers", {
      data: { name: "a11y trigger", target_type: "prompt", prompt_template_id: prompt, agent_profile_id: profile },
    });
    triggers = (await (await request.get("/api/v1/triggers")).json()) as { items: { id: string }[] };
  }
  const workflow = await firstId(request, "/workflows");
  return [
    "/",
    "/welcome",
    "/runs",
    `/runs/${agentRun!.id}`,
    `/runs/${agentRun!.id}?tab=inputs`,
    `/runs/${agentRun!.id}?tab=artifacts`,
    `/runs/${workflowRun!.id}?tab=workflow`,
    `/runs/compare?a=${fork!.parent_run_id}&b=${fork!.id}`,
    `/runs/${evidenceRun!.id}?tab=findings&finding=F2`,
    `/runs/${evidenceRun!.id}?tab=findings&finding=F4`,
    `/runs/${agentRun!.id}?tab=findings`,
    "/findings",
    `/runs/${evidenceRun!.id}?tab=context`,
    "/workflows",
    "/workflows/new",
    `/workflows/${workflow}`,
    `/workflows/${workflow}/edit`,
    "/workflows/evaluations",
    "/schedules",
    "/schedules/new",
    `/schedules/${await firstId(request, "/schedules")}`,
    "/prompts",
    "/prompts/new",
    `/prompts/${await firstId(request, "/prompts")}`,
    "/agents",
    "/agents/new",
    `/agents/${await firstId(request, "/agent-profiles")}`,
    "/projects",
    "/projects/new",
    `/projects/${await firstId(request, "/projects")}`,
    "/approvals",
    "/settings",
    "/settings/triggers",
    `/settings/triggers/${triggers.items[0]!.id}`,
    "/settings/limits",
    "/settings/retention",
  ];
}

async function open(page: Page, path: string) {
  await page.goto(path);
  await expect(page.locator("main h1").first()).toBeVisible();
  await page.waitForTimeout(400);
}

async function scan(page: Page, path: string): Promise<string[]> {
  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .exclude(".react-flow__panel")
    .analyze();
  return results.violations
    .filter((v) => v.impact === "serious" || v.impact === "critical")
    .map((v) => `${path}: ${v.id} (${v.impact}) ${v.nodes.slice(0, 3).map((n) => n.target.join(" ")).join(" | ")}`);
}

for (const scheme of ["light", "dark"] as const) {
  for (const width of [1280, 768] as const) {
    test(`no serious accessibility violations (${scheme}, ${width}px)`, async ({ page, request }) => {
      test.setTimeout(240_000);
      await page.setViewportSize({ width, height: 900 });
      await page.emulateMedia({ colorScheme: scheme });
      const failures: string[] = [];
      for (const path of await allPages(request)) {
        await open(page, path);
        expect(await page.evaluate(() => document.documentElement.dataset.theme)).toBe(scheme);
        failures.push(...(await scan(page, path)));
      }
      expect(failures, failures.join("\n")).toEqual([]);
    });
  }
}

test("tablet width: no page scrolls sideways", async ({ page, request }) => {
  test.setTimeout(180_000);
  await page.setViewportSize({ width: 768, height: 1024 });
  const overflowing: string[] = [];
  for (const path of await allPages(request)) {
    await open(page, path);
    const [scroll, client] = await page.evaluate(() => [
      document.documentElement.scrollWidth,
      document.documentElement.clientWidth,
    ]);
    if (scroll > client + 1) overflowing.push(`${path}: ${scroll}px content in a ${client}px viewport`);
  }
  expect(overflowing, overflowing.join("\n")).toEqual([]);
});

test("keyboard: skip link, navigation, and visible focus", async ({ page }) => {
  await page.goto("/runs");
  await expect(page.locator("main h1").first()).toBeVisible();
  await page.keyboard.press("Tab");
  const skip = page.getByRole("link", { name: "Skip to content" });
  await expect(skip).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.locator("#main")).toBeFocused();
  const nav = page.getByRole("navigation", { name: "Main" });
  const settings = nav.getByRole("link", { name: "Settings" });
  await settings.focus();
  const outline = await settings.evaluate((el) => getComputedStyle(el).outlineStyle);
  expect(outline).not.toBe("none");
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/\/settings$/);
  const res = await page.request.get("/api/v1/runs?limit=1&status=succeeded");
  const run = ((await res.json()) as { items: { id: string }[] }).items[0];
  expect(run).toBeTruthy();
  await page.goto(`/runs/${run!.id}`);
  const first = page.getByRole("tab").first();
  await first.focus();
  await page.keyboard.press("ArrowRight");
  await expect(page.getByRole("tab").nth(1)).toBeFocused();
  await expect(page.getByRole("tab").nth(1)).toHaveAttribute("aria-selected", "true");
});

test("tablet navigation drawer traps focus, closes on Escape, and restores focus", async ({ page }) => {
  await page.setViewportSize({ width: 768, height: 1024 });
  await page.goto("/runs");
  const menu = page.getByRole("button", { name: "Open navigation" });
  await menu.focus();
  await page.keyboard.press("Enter");
  const drawer = page.getByRole("dialog", { name: "Navigation" });
  await expect(drawer).toBeVisible();
  // Focus moves into the drawer and stays there while tabbing past the last control.
  await expect(drawer.getByRole("button", { name: "Close navigation" })).toBeFocused();
  for (let i = 0; i < 20; i += 1) {
    await page.keyboard.press("Tab");
    const inside = await drawer.evaluate((d) => d.contains(document.activeElement));
    expect(inside, `focus left the drawer after ${i + 1} Tab presses`).toBe(true);
  }
  await page.keyboard.press("Escape");
  await expect(drawer).toBeHidden();
  await expect(menu).toBeFocused();
  // Choosing a destination closes the drawer and navigates.
  await menu.click();
  await drawer.getByRole("link", { name: "Schedules" }).click();
  await expect(page).toHaveURL(/\/schedules$/);
  await expect(drawer).toBeHidden();
});
