/** Copyable examples for calling a webhook trigger (pure, unit-tested). */

export const TOKEN_PLACEHOLDER = "$AGENTTAPE_WEBHOOK_TOKEN";

type Json = string | number | boolean | null | Json[] | { [key: string]: Json };

/**
 * A sample payload that satisfies ``$.a.b`` style mappings: each mapped variable becomes a
 * placeholder string at its path. Paths with indexes or quoted keys are skipped.
 */
export function samplePayload(mapping: Record<string, string>): Record<string, Json> {
  const root: Record<string, Json> = {};
  for (const [variable, path] of Object.entries(mapping)) {
    if (!/^\$(\.[A-Za-z_][A-Za-z0-9_-]*)+$/.test(path)) continue;
    const parts = path.slice(2).split(".");
    let node: Record<string, Json> = root;
    parts.forEach((part, i) => {
      if (i === parts.length - 1) {
        node[part] = `<${variable}>`;
      } else {
        const next = node[part];
        if (typeof next !== "object" || next === null || Array.isArray(next)) node[part] = {};
        node = node[part] as Record<string, Json>;
      }
    });
  }
  return root;
}

function shellQuote(text: string): string {
  return `'${text.replace(/'/g, `'"'"'`)}'`;
}

export function curlExample(url: string, mapping: Record<string, string>, token?: string): string {
  const body = JSON.stringify(samplePayload(mapping));
  const auth = token ? `Bearer ${token}` : `Bearer ${TOKEN_PLACEHOLDER}`;
  return [
    `curl -X POST ${shellQuote(url)} \\`,
    `  -H ${token ? shellQuote(`Authorization: ${auth}`) : `"Authorization: ${auth}"`} \\`,
    `  -H 'Content-Type: application/json' \\`,
    `  -H "Idempotency-Key: $(date +%s)" \\`,
    `  -d ${shellQuote(body)}`,
  ].join("\n");
}

/** A GitHub Actions step that calls the trigger with a repository_dispatch-shaped body. */
export function githubActionsExample(url: string, mapping: Record<string, string>): string {
  const payload = samplePayload(mapping);
  const clientPayload = (payload.client_payload as Record<string, Json> | undefined) ?? {};
  const withRefs: Record<string, Json> = {};
  for (const key of Object.keys(clientPayload)) {
    withRefs[key] = key === "branch" ? "${{ github.ref_name }}" : `<${key}>`;
  }
  const body = JSON.stringify({ event_type: "agenttape", client_payload: withRefs });
  return [
    "- name: Start an AgentTape run",
    "  run: |",
    `    curl -fsS -X POST "${url}" \\`,
    '      -H "Authorization: Bearer ${{ secrets.AGENTTAPE_WEBHOOK_TOKEN }}" \\',
    '      -H "Content-Type: application/json" \\',
    '      -H "Idempotency-Key: ${{ github.run_id }}-${{ github.run_attempt }}" \\',
    `      -d '${body}'`,
  ].join("\n");
}

/** Trigger a GitHub repository_dispatch event (delivered to AgentTape by a repo webhook). */
export function githubDispatchExample(mapping: Record<string, string>): string {
  const payload = samplePayload(mapping);
  const clientPayload = (payload.client_payload as Record<string, Json> | undefined) ?? {};
  const fields = Object.keys(clientPayload).map((k) => `  -f 'client_payload[${k}]=<${k}>'`);
  return [
    "gh api repos/OWNER/REPO/dispatches \\",
    "  -f event_type=agenttape" + (fields.length ? " \\" : ""),
    ...fields.map((f, i) => (i < fields.length - 1 ? `${f} \\` : f)),
  ].join("\n");
}
