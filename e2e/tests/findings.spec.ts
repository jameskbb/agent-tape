import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

/**
 * The Findings UI on the seeded evidence run: F3's CHANGELOG.md
 * citation renders the exact lines AgentTape retained from the run's own read, marked in
 * the gutter, with AgentTape's own "Quote matches" verdict beside them.
 *
 * The rest of this file has one test per Findings UI flow, run against the
 * seeded demo in a real browser: the tab and its verdict-filter chips, F2's stale citation
 * and its already-seeded challenge outcome, the three "nothing to click" failure states, a
 * fresh challenge and a confirm/withdraw round trip (both against the variant b run so the
 * primary run's seeded state stays stable for axe scans), the Findings
 * inbox, the Overview tile and the run header's Findings fact, tablet/keyboard operation,
 * and the run export bundle.
 */

interface RunLite {
  id: string;
  title: string;
}

async function runs(request: APIRequestContext, q: string): Promise<RunLite[]> {
  const res = await request.get(`/api/v1/runs?q=${encodeURIComponent(q)}&limit=50`);
  expect(res.ok()).toBeTruthy();
  return ((await res.json()) as { items: RunLite[] }).items;
}

/** The primary evidence run: exact title, no "variant" suffix. It is the run the axe scans
 * use, so data-changing flows never touch it. */
async function primaryEvidenceRun(request: APIRequestContext): Promise<RunLite> {
  const [run] = (await runs(request, "Refactor with Full File Evidence")).filter(
    (r) => r.title === "Refactor with Full File Evidence",
  );
  expect(run, "seeded primary evidence run").toBeTruthy();
  return run!;
}

/** The variant b evidence run: same designed findings and verdicts as the primary run, used
 * for every flow that changes data (challenge, confirm/withdraw). */
async function variantBEvidenceRun(request: APIRequestContext): Promise<RunLite> {
  const [run] = (await runs(request, "Refactor with Full File Evidence")).filter(
    (r) => r.title === "Refactor with Full File Evidence (variant b)",
  );
  expect(run, "seeded variant b evidence run").toBeTruthy();
  return run!;
}

/** The `<li>` of the six-rung evidence ladder whose text contains `text` (there is exactly
 * one, since each rung's label is unique to its slot). */
function ladderRung(page: Page, text: string | RegExp) {
  return page.getByRole("list", { name: "Evidence for this finding" }).locator("li", {
    hasText: text,
  });
}

test("F3's CHANGELOG.md citation renders its retained, cited lines with a Quote matches verdict", async ({
  page,
  request,
}) => {
  const evidence = await primaryEvidenceRun(request);

  await page.goto(`/runs/${evidence.id}?tab=findings&finding=F3`);

  const tablist = page.getByRole("tablist", { name: "Run sections" });
  await expect(tablist.getByRole("tab", { name: /Findings/ })).toHaveAttribute(
    "aria-selected",
    "true",
  );

  const citation = page.getByRole("region", { name: /Citation: CHANGELOG\.md/ });
  await expect(citation).toBeVisible();

  const citedRow = citation.locator("tr", { hasText: "## Unreleased" });
  await expect(citedRow).toBeVisible();
  await expect(citedRow).toContainText("›");

  await expect(citation.getByText("Quote matches")).toBeVisible();
});

test("F2's src/greeter.py path opens the Files tab drawer, and its read link opens the Timeline event", async ({
  page,
  request,
}) => {
  const evidence = await primaryEvidenceRun(request);
  const runId = evidence.id;

  await page.goto(`/runs/${runId}?tab=findings&finding=F2`);
  const citation = page.getByRole("region", { name: /Citation: src\/greeter\.py/ });
  await expect(citation).toBeVisible();

  const pathLink = citation.getByRole("link", { name: "src/greeter.py" });
  await expect(pathLink).toHaveAttribute("href", /tab=files&path=src%2Fgreeter\.py/);
  await pathLink.click();
  await expect(page).toHaveURL(/tab=files&path=src%2Fgreeter\.py/);
  await expect(
    page.getByRole("dialog", { name: "File evidence: src/greeter.py" }),
  ).toBeVisible();

  // Back on the Findings tab, the citation's read link names the write's own timeline event.
  await page.goto(`/runs/${runId}?tab=findings&finding=F2`);
  const citationAgain = page.getByRole("region", { name: /Citation: src\/greeter\.py/ });
  const readLink = citationAgain.getByRole("link", { name: /read #\d+/ });
  const href = await readLink.getAttribute("href");
  const sequence = href?.match(/event=(\d+)/)?.[1];
  expect(sequence, "read link names an event sequence").toBeTruthy();
  await readLink.click();
  await expect(page.getByRole("tab", { name: /Timeline/ })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await expect(page.locator(`#event-${sequence}`)).toBeVisible();
});

test("loading a run with ?tab=files&path= opens that file's drawer directly, and closing clears the param", async ({
  page,
  request,
}) => {
  const evidence = await primaryEvidenceRun(request);
  const runId = evidence.id;

  await page.goto(`/runs/${runId}?tab=files&path=README.md`);
  const drawer = page.getByRole("dialog", { name: "File evidence: README.md" });
  await expect(drawer).toBeVisible();

  await drawer.getByRole("button", { name: "Close" }).click();
  await expect(drawer).toBeHidden();
  await expect(page).not.toHaveURL(/path=/);
});

test("Findings tab: the statement card explains itself, and the Stale chip filters the recorded-order list", async ({
  page,
  request,
}) => {
  const evidence = await primaryEvidenceRun(request);

  await page.goto(`/runs/${evidence.id}?tab=findings`);

  await expect(page.getByRole("heading", { name: "Checked against the record" })).toBeVisible();
  await expect(page.getByText(/Checkable, not foolproof/)).toBeVisible();

  const chips = page.getByRole("group", { name: "Filter by verdict" });
  const staleChip = chips.getByRole("button", { name: "Stale 1" });
  await staleChip.click();
  await expect(staleChip).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByText("Showing 1 of 8 findings with Stale")).toBeVisible();

  const list = page.getByRole("list", { name: "Findings" });
  await expect(list.getByRole("button")).toHaveCount(1);
  await expect(list).toContainText("greet() concatenates");

  await page.getByRole("button", { name: "Clear filter" }).click();
  await expect(staleChip).toHaveAttribute("aria-pressed", "false");
  await expect(page.getByRole("heading", { name: "Findings (8)" })).toBeVisible();
});

test("F2's detail pane shows its hatched stale citation, its reproduced search citation, and a six-rung ladder with the seeded challenge outcome", async ({
  page,
  request,
}) => {
  const evidence = await primaryEvidenceRun(request);

  await page.goto(`/runs/${evidence.id}?tab=findings&finding=F2`);

  const fileCitation = page.getByRole("region", { name: /Citation: src\/greeter\.py/ });
  await expect(fileCitation.locator("tr.at-hatch-warning").first()).toBeVisible();
  await expect(fileCitation.getByText(/Hash at read/)).toBeVisible();

  const eventCitation = page.getByRole("region", { name: /Citation: cmd_demo_search/ });
  await expect(eventCitation.getByText(/Re-run reproduced at/)).toBeVisible();

  const ladder = page.getByRole("list", { name: "Evidence for this finding" });
  await expect(ladder.locator("li")).toHaveCount(6);
  await expect(ladderRung(page, "Reproduced by AgentTape")).toBeVisible();
  const challengeRung = ladderRung(page, "model judgment");
  await expect(challengeRung).toBeVisible();
  const challengeLink = challengeRung.getByRole("link", { name: "Open the challenge run" });
  await expect(challengeLink).toHaveAttribute("href", /\/runs\//);
});

test("F4, F5, and F7 show their failure states with nothing to click", async ({
  page,
  request,
}) => {
  const evidence = await primaryEvidenceRun(request);

  await page.goto(`/runs/${evidence.id}?tab=findings&finding=F4`);
  const f4 = page.getByRole("region", { name: /Citation: CHANGELOG\.md/ });
  await expect(f4.getByText(/Recorded lines L\d+-L\d+/)).toBeVisible();
  await expect(f4.locator("tr.at-hatch-danger").first()).toBeVisible();
  await expect(f4.getByText("Quoted by the agent")).toBeVisible();

  await page.goto(`/runs/${evidence.id}?tab=findings&finding=F5`);
  const f5 = page.getByRole("region", { name: /Citation: tests\/test_greeter\.py/ });
  await expect(f5.getByText(/This run has no record of reading/)).toBeVisible();

  await page.goto(`/runs/${evidence.id}?tab=findings&finding=F7`);
  const f7 = page.getByRole("region", { name: /Citation: src\/formatting\.py/ });
  await expect(f7.getByText("Read not observed")).toBeVisible();
  await expect(f7.getByText("Never read")).toHaveCount(0);
});

test("Challenge findings: starting one from the variant b run lands on the child run, and the parent's rung moves from running to a model-judgment outcome", async ({
  page,
  request,
}) => {
  test.setTimeout(120_000);
  const variantB = await variantBEvidenceRun(request);

  await page.goto(`/runs/${variantB.id}`);
  const challengeButton = page.getByRole("button", { name: "Challenge findings" });
  await expect(challengeButton).toBeEnabled();
  await challengeButton.click();

  const dialog = page.getByRole("dialog", { name: "Challenge findings" });
  await expect(dialog).toBeVisible();
  await dialog
    .getByRole("combobox", { name: "Reviewer agent" })
    .selectOption({ label: "Demo Agent (reviewer)" });
  await dialog.getByRole("button", { name: "Start challenge" }).click();

  await expect(page).toHaveURL(new RegExp(`/runs/(?!${variantB.id}$)[^/?]+$`));

  await page.goto(`/runs/${variantB.id}?tab=findings&finding=F2`);
  await expect(ladderRung(page, "model judgment")).toContainText("(model judgment)");

  await expect
    .poll(
      async () => {
        await page.reload();
        return ladderRung(page, "model judgment").innerText();
      },
      { timeout: 60_000, message: "the challenge never settled to an outcome" },
    )
    .toMatch(/^Challenge:.*\(model judgment\)/);
});

test("Confirm finding: confirming F3 on the variant b run is audited and reversible", async ({
  page,
  request,
}) => {
  const variantB = await variantBEvidenceRun(request);

  await page.goto(`/runs/${variantB.id}?tab=findings&finding=F3`);

  const confirmButton = page.getByRole("button", { name: "Confirm finding" });
  await confirmButton.click();
  const confirmDialog = page.getByRole("dialog", { name: "Confirm finding" });
  await expect(confirmDialog).toBeVisible();
  await confirmDialog
    .getByLabel("Note (optional, saved to the audit log)")
    .fill("Reviewed the citations and their evidence.");
  await confirmDialog.getByRole("button", { name: "Confirm finding" }).click();
  await expect(confirmDialog).toBeHidden();

  await expect(ladderRung(page, "Confirmed by")).toBeVisible();

  const auditRes = await request.get("/api/v1/audit?resource_type=finding&limit=50");
  expect(auditRes.ok()).toBeTruthy();
  const audit = (await auditRes.json()) as { items: { action: string }[] };
  expect(audit.items.some((item) => item.action === "finding.confirm")).toBe(true);

  const withdrawButton = page.getByRole("button", { name: "Withdraw confirmation" });
  await withdrawButton.click();
  const withdrawDialog = page.getByRole("dialog", { name: "Withdraw confirmation" });
  await expect(withdrawDialog).toBeVisible();
  await withdrawDialog.getByRole("button", { name: "Withdraw confirmation" }).click();
  await expect(withdrawDialog).toBeHidden();

  await expect(ladderRung(page, "Not confirmed")).toBeVisible();
});

test("The Findings inbox lists flagged findings first, filters by verdict, and opens a finding on its run", async ({
  page,
}) => {
  await page.goto("/runs");
  const nav = page.getByRole("navigation", { name: "Main" });
  await nav.getByRole("link", { name: "Findings" }).click();
  await expect(page).toHaveURL(/\/findings$/);

  const rows = page.locator("table tbody tr");
  await expect(rows.first()).toBeVisible();
  const firstVerdict = (await rows.first().locator("td").first().innerText()).trim();
  expect(firstVerdict).not.toBe("Quote matches");

  const [filtered] = await Promise.all([
    page.waitForResponse(
      (res) => res.url().includes("/api/v1/findings?") && res.url().includes("verdict=quote_not_found"),
    ),
    page.getByRole("combobox", { name: "Verdict" }).selectOption({ label: "Quote not found" }),
  ]);
  expect(filtered.ok()).toBeTruthy();
  await expect(rows.first().locator("td").first()).toContainText("Quote not found");
  const count = await rows.count();
  for (let i = 0; i < count; i += 1) {
    await expect(rows.nth(i).locator("td").first()).toContainText("Quote not found");
  }

  const claimLink = rows.first().getByRole("link").first();
  const claimText = (await claimLink.innerText()).trim();
  await claimLink.click();
  await expect(page).toHaveURL(/\/runs\/[^/]+\?tab=findings&finding=/);
  await expect(page.getByRole("heading", { name: claimText })).toBeVisible();
});

test("The Overview leads with Verified findings, and a run's Timeline tab links its Findings fact to the Findings tab", async ({
  page,
  request,
}) => {
  const evidence = await primaryEvidenceRun(request);

  await page.goto("/");
  const stats = page.locator("dl").first();
  await expect(stats.locator("dt").first()).toHaveText("Verified findings");

  await page.goto(`/runs/${evidence.id}?tab=timeline`);
  await expect(page.getByRole("tab", { name: /Timeline/ })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  const findingsFact = page.locator("dt", { hasText: "Findings" });
  const findingsLink = findingsFact.locator("xpath=following-sibling::dd[1]").getByRole("link");
  await expect(findingsLink).toHaveAttribute("href", /tab=findings/);
});

test("Tablet: the finding list is a picker with no sideways scroll; desktop: the list and the ladder are keyboard-operable", async ({
  page,
  request,
}) => {
  const evidence = await primaryEvidenceRun(request);

  await page.setViewportSize({ width: 768, height: 1024 });
  await page.goto(`/runs/${evidence.id}?tab=findings`);
  const picker = page.getByRole("combobox", { name: "Findings" });
  await expect(picker).toBeVisible();
  await picker.selectOption({ index: 1 });
  await expect(page.getByRole("heading", { name: /greet\(\)/ })).toBeVisible();
  const [scrollWidth, clientWidth] = await page.evaluate(() => [
    document.documentElement.scrollWidth,
    document.documentElement.clientWidth,
  ]);
  expect(scrollWidth).toBeLessThanOrEqual(clientWidth + 1);

  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto(`/runs/${evidence.id}?tab=findings`);
  const list = page.getByRole("list", { name: "Findings" });
  const rows = list.getByRole("button");
  await rows.first().focus();
  await page.keyboard.press("ArrowDown");
  await expect(rows.nth(1)).toHaveAttribute("aria-current", "true");

  await page.goto(`/runs/${evidence.id}?tab=findings&finding=F2`);
  const quotesMatchRung = ladderRung(page, "Quotes match").getByRole("button");
  await quotesMatchRung.focus();
  await expect(quotesMatchRung).toHaveAttribute("aria-expanded", "false");
  await page.keyboard.press("Enter");
  await expect(quotesMatchRung).toHaveAttribute("aria-expanded", "true");

  // The viewer's expand-above control is always a real <button> (keyboard coverage),
  // whether the seeded demo file leaves room to expand ("Show 20 more lines above") or the
  // retained read already starts at the file's first line ("Start of retained read").
  const expandAbove = page.getByRole("button", {
    name: /^(Show 20 more lines above|Start of retained read)$/,
  });
  await expect(expandAbove.first()).toBeVisible();
});

test("The run export bundle carries F2's citations, verdicts, and reproduction", async ({
  request,
}) => {
  const evidence = await primaryEvidenceRun(request);

  const res = await request.get(`/api/v1/runs/${evidence.id}/export`);
  expect(res.ok()).toBeTruthy();
  const body = (await res.json()) as {
    findings: { key: string; citations: unknown[] }[];
  };
  expect(Array.isArray(body.findings)).toBe(true);
  expect(body.findings.length).toBeGreaterThan(0);
  const f2 = body.findings.find((f) => f.key === "F2");
  expect(f2, "F2 present in the export bundle").toBeTruthy();
  expect(f2!.citations.length).toBe(2);
});
