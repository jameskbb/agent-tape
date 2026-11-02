/** Settings, Runtime: Herdr status, machines, integrations, defaults, capabilities, and a
 * connection test. Everything is read from the server; nothing here changes Herdr. */
import { CheckCircle2, XCircle } from "lucide-react";
import { Link } from "react-router";

import { describeError } from "@/api/client";
import { Page } from "@/components/layout/Page";
import { Badge, Button, Card, ErrorState, LoadingBlock, Unavailable } from "@/components/ui";

import { useHerdrStatus, useHerdrTest, type HerdrStatus } from "./api";
import { Row, SupportBadge } from "./parts";

const DEFAULT_LABELS: Record<string, Record<string, string>> = {
  workspace_policy: {
    new: "A new AgentTape-owned workspace per attempt",
    reuse: "Reuse a configured workspace or agent",
  },
  cleanup: {
    keep: "Keep workspaces after the run (close only when a profile says close_owned)",
    close_owned: "Close AgentTape-owned workspaces when the attempt ends",
  },
  fallback: {
    none: "None: fail clearly when Herdr is unavailable",
    local: "Run locally, only before the attempt starts",
  },
};

const RUNTIME_LABELS: Record<string, string> = {
  "herdr:claude": "Herdr: Claude Code",
  "herdr:codex": "Herdr: Codex",
  "herdr:demo": "Herdr: demo agent",
  "local:claude-code": "Local: Claude Code",
  "local:codex-cli": "Local: Codex",
  "local:generic-command": "Local: command",
  "local:demo": "Local: Demo Agent",
};

function CapabilityTable({ status }: { status: HerdrStatus }) {
  const keys = Object.keys(status.runtime_capabilities);
  const names = status.runtime_capabilities[keys[0] ?? ""] ?? [];
  return (
    <div className="overflow-x-auto" tabIndex={0} aria-label="Runtime capabilities table">
      <table className="w-full min-w-[48rem] text-sm" aria-label="Runtime capabilities">
        <thead>
          <tr className="border-b border-line text-left text-xs text-muted">
            <th scope="col" className="py-2 pr-3 font-medium">
              Capability
            </th>
            {keys.map((k) => (
              <th key={k} scope="col" className="py-2 pr-3 font-medium">
                {RUNTIME_LABELS[k] ?? k}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {names.map((cap) => (
            <tr key={cap.name} className="border-b border-line last:border-0">
              <th scope="row" className="py-2 pr-3 text-left font-normal">
                {cap.description}
              </th>
              {keys.map((k) => {
                const c = status.runtime_capabilities[k]?.find((x) => x.name === cap.name);
                return (
                  <td key={k} className="py-2 pr-3">
                    {c ? <SupportBadge cap={c} /> : <Unavailable />}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
      <p className="mt-2 text-xs text-muted">Hover a badge for the exact limitation.</p>
    </div>
  );
}

function ConnectionTest() {
  const test = useHerdrTest();
  return (
    <div className="flex flex-col gap-3">
      <div>
        <Button onClick={() => test.mutate()} disabled={test.isPending}>
          {test.isPending ? "Testing..." : "Test connection"}
        </Button>
      </div>
      {test.isError ? (
        <p className="text-sm text-[var(--st-failed)]">{describeError(test.error)}</p>
      ) : null}
      {test.data ? (
        <ul className="flex flex-col gap-2" aria-label="Connection test results">
          {test.data.checks.map((c) => (
            <li key={c.name} className="flex gap-2 text-sm">
              {c.ok ? (
                <CheckCircle2
                  aria-label="Passed"
                  className="mt-0.5 size-4 shrink-0 text-[var(--st-succeeded)]"
                />
              ) : (
                <XCircle
                  aria-label="Failed"
                  className="mt-0.5 size-4 shrink-0 text-[var(--st-failed)]"
                />
              )}
              <span>
                <span className="font-medium">{c.name}</span>: {c.detail}
                {c.hint ? <span className="block text-muted">{c.hint}</span> : null}
              </span>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

export function RuntimeSettingsPage() {
  const status = useHerdrStatus();
  return (
    <Page
      title="Runtime"
      breadcrumb={
        <Link to="/settings" className="hover:text-ink">
          Settings
        </Link>
      }
      description="Herdr is where agents live; AgentTape records why they ran and what they changed. Choose the Herdr runtime in an agent profile (adapter: Herdr runtime)."
    >
      {status.isPending ? (
        <LoadingBlock label="Checking Herdr" />
      ) : status.isError ? (
        <ErrorState error={status.error} onRetry={() => void status.refetch()} />
      ) : (
        <>
          <Card
            title="Herdr"
            description={status.data.message}
            actions={
              <Button variant="secondary" size="sm" onClick={() => void status.refetch()}>
                Refresh
              </Button>
            }
          >
            <dl className="divide-y divide-line">
              <Row label="Installed">
                {status.data.installed ? (
                  <Badge tone="success">
                    Installed{status.data.cli_version ? ` ${status.data.cli_version}` : ""}
                  </Badge>
                ) : (
                  <Badge>Not found on PATH</Badge>
                )}
              </Row>
              <Row label="Server">
                {status.data.reachable ? (
                  <Badge tone="success">Reachable</Badge>
                ) : (
                  <Badge tone="warning">Unavailable</Badge>
                )}
              </Row>
              <Row label="Version">
                {status.data.version ? (
                  `${status.data.version} (API protocol ${status.data.protocol})`
                ) : (
                  <Unavailable reason="Server not reachable" />
                )}
              </Row>
              <Row label="Session">{status.data.session}</Row>
              <Row label="Machine">{status.data.machine}</Row>
            </dl>
            <div className="mt-4 border-t border-line pt-4">
              <ConnectionTest />
            </div>
          </Card>

          <div className="grid gap-6 lg:grid-cols-2">
            <Card
              title="Machines"
              description="Saved Herdr machines (read-only). Add them with herdr machine add; AgentTape never opens SSH itself."
            >
              {status.data.machines === null ? (
                <Unavailable reason="The Herdr CLI is not available" />
              ) : status.data.machines.length === 0 ? (
                <p className="text-sm text-muted">
                  Only this machine. Runs use the session socket AgentTape is configured with.
                </p>
              ) : (
                <ul className="flex flex-col gap-1 text-sm">
                  {status.data.machines.map((m, i) => (
                    <li key={i} className="flex flex-wrap gap-2">
                      <span className="font-medium">{m.label ?? m.name ?? "machine"}</span>
                      {m.ssh_target ? <code>{m.ssh_target}</code> : null}
                      {m.enabled === false ? <Badge>Disabled</Badge> : null}
                    </li>
                  ))}
                </ul>
              )}
            </Card>
            <Card title="Agent integrations" description="Reported by the Herdr server.">
              {status.data.integrations === null ? (
                <Unavailable reason="Server not reachable" />
              ) : (
                <ul className="flex flex-wrap gap-2">
                  {status.data.integrations
                    .filter((i) => i.available || ["claude", "codex"].includes(i.target))
                    .map((i) => (
                      <li key={i.target}>
                        <Badge tone={i.available ? "success" : "neutral"}>
                          {i.label}: {i.state.replaceAll("_", " ")}
                        </Badge>
                      </li>
                    ))}
                </ul>
              )}
            </Card>
          </div>

          <Card
            title="Defaults"
            description="Used by Herdr agent profiles unless the profile sets its own policy."
          >
            <dl className="divide-y divide-line">
              <Row label="Workspace reuse">
                {DEFAULT_LABELS.workspace_policy?.[status.data.defaults.workspace_policy ?? ""]}
              </Row>
              <Row label="Cleanup">
                {DEFAULT_LABELS.cleanup?.[status.data.defaults.cleanup ?? ""]}
              </Row>
              <Row label="Fallback">
                {DEFAULT_LABELS.fallback?.[status.data.defaults.fallback ?? ""]}
              </Row>
            </dl>
          </Card>

          <Card
            title="Capabilities"
            description="What AgentTape can do and observe through each runtime. Degraded means it works with a stated limitation."
          >
            <CapabilityTable status={status.data} />
          </Card>
        </>
      )}
    </Page>
  );
}
