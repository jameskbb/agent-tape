import { Archive, ClipboardCheck, Compass, Gauge, TerminalSquare, Webhook } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate } from "react-router";

import { describeError, getApiToken, setApiToken } from "@/api/client";
import { useAdapters, useSystemInfo } from "@/api/hooks";
import { ThemeSwitch } from "@/app/Shell";
import { Page } from "@/components/layout/Page";
import {
  Badge,
  Button,
  Card,
  ErrorState,
  LoadingBlock,
  Unavailable,
  useToast,
} from "@/components/ui";
import { useOnboardingAction } from "@/features/onboarding/api";
import { formatBytes } from "@/lib/format";

const SETTINGS_LINKS = [
  {
    to: "/settings/triggers",
    label: "Webhook triggers",
    description: "Start saved prompts and workflows from GitHub, CI, or scripts.",
    Icon: Webhook,
  },
  {
    to: "/settings/runtime",
    label: "Runtime",
    description: "Herdr status, machines, capabilities, and a connection test.",
    Icon: TerminalSquare,
  },
  {
    to: "/settings/limits",
    label: "Limits",
    description: "Concurrent runs and default budgets for every run.",
    Icon: Gauge,
  },
  {
    to: "/settings/retention",
    label: "Data retention",
    description: "How long finished runs are kept, with a preview before any cleanup.",
    Icon: Archive,
  },
  {
    to: "/workflows/evaluations",
    label: "Evaluators",
    description: "Reusable, versioned checks and suites that grade runs.",
    Icon: ClipboardCheck,
  },
];

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid gap-1 py-2 sm:grid-cols-[12rem_minmax(0,1fr)] sm:gap-4">
      <dt className="text-sm text-muted">{label}</dt>
      <dd className="min-w-0 break-words">{children}</dd>
    </div>
  );
}

export function SettingsPage() {
  const info = useSystemInfo();
  const adapters = useAdapters();
  const toast = useToast();
  const [token, setToken] = useState(() => getApiToken() ?? "");
  const onboarding = useOnboardingAction();
  const navigate = useNavigate();

  return (
    <Page
      title="Settings"
      description="How this AgentTape instance is configured. Values come from the server; secrets are never shown."
    >
      <Card title="Configure" description="Automation, safety limits, and housekeeping.">
        <ul className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {SETTINGS_LINKS.map(({ to, label, description, Icon }) => (
            <li key={to}>
              <Link
                to={to}
                className="flex h-full gap-3 rounded-[var(--radius-control)] border border-line p-3 hover:bg-sunken"
              >
                <Icon aria-hidden className="mt-0.5 size-4 shrink-0 text-accent" />
                <span>
                  <span className="block font-medium">{label}</span>
                  <span className="block text-sm text-muted">{description}</span>
                </span>
              </Link>
            </li>
          ))}
          <li>
            <button
              type="button"
              disabled={onboarding.isPending}
              onClick={() =>
                onboarding.mutate("reset", {
                  onSuccess: () => void navigate("/welcome"),
                  onError: (e) =>
                    toast.show(`Could not re-open the setup: ${describeError(e)}`, "error"),
                })
              }
              className="flex h-full w-full gap-3 rounded-[var(--radius-control)] border border-line p-3 text-left hover:bg-sunken disabled:opacity-60"
            >
              <Compass aria-hidden className="mt-0.5 size-4 shrink-0 text-accent" />
              <span>
                <span className="block font-medium">First-run setup</span>
                <span className="block text-sm text-muted">
                  Walk through a project, an agent, and a prompt again.
                </span>
              </span>
            </button>
          </li>
        </ul>
      </Card>

      <Card title="Appearance">
        <div className="flex items-center justify-between gap-4">
          <p className="text-sm text-muted">Follow the system setting, or pin light or dark.</p>
          <ThemeSwitch />
        </div>
      </Card>

      <Card title="Server" description="Read from the running API.">
        {info.isPending ? (
          <LoadingBlock />
        ) : info.isError ? (
          <ErrorState error={info.error} onRetry={() => void info.refetch()} />
        ) : (
          <dl className="divide-y divide-line">
            <Row label="Version">{info.data.version}</Row>
            <Row label="Data directory">
              <code>{info.data.data_dir}</code>
            </Row>
            <Row label="Database">{info.data.database}</Row>
            <Row label="Listening on">
              <code>
                {info.data.host}:{info.data.port}
              </code>
            </Row>
            <Row label="API token">
              {info.data.token_required ? (
                <Badge tone="warning">Required for API calls</Badge>
              ) : (
                <Badge>Not required on loopback</Badge>
              )}
            </Row>
            <Row label="Allowed project roots">
              <ul className="flex flex-col gap-0.5">
                {info.data.allowed_roots.map((root) => (
                  <li key={root}>
                    <code>{root}</code>
                  </li>
                ))}
              </ul>
            </Row>
            <Row label="Raw log cap">
              {info.data.limits.max_raw_log_bytes === undefined ? (
                <Unavailable reason="Not reported by the server" />
              ) : (
                `${formatBytes(info.data.limits.max_raw_log_bytes)} per stream`
              )}
            </Row>
            <Row label="Default timeout">
              {info.data.limits.default_timeout_seconds === undefined ? (
                <Unavailable reason="Not reported by the server" />
              ) : (
                `${info.data.limits.default_timeout_seconds} seconds`
              )}
            </Row>
          </dl>
        )}
      </Card>

      <Card
        title="Adapters"
        description="Discovered on this machine. Discovery never needs credentials."
      >
        {adapters.isPending ? (
          <LoadingBlock />
        ) : adapters.isError ? (
          <ErrorState error={adapters.error} onRetry={() => void adapters.refetch()} />
        ) : (
          <ul className="divide-y divide-line">
            {adapters.data.map((a) => (
              <li key={a.name} className="flex flex-wrap items-center justify-between gap-2 py-2.5">
                <div>
                  <p className="font-medium">{a.display_name}</p>
                  <p className="text-sm text-muted">{a.message ?? a.description}</p>
                </div>
                <Badge
                  tone={
                    a.status === "ready" || a.status === "installed"
                      ? "success"
                      : a.status === "misconfigured"
                        ? "warning"
                        : "neutral"
                  }
                >
                  {a.status}
                  {a.version ? ` ${a.version}` : ""}
                </Badge>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card
        title="API token for this tab"
        description="Only needed when AgentTape listens beyond this machine. Kept for this browser tab only; you paste it again after closing the tab."
      >
        <form
          className="flex flex-wrap items-end gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            setApiToken(token.trim() || null);
            toast.show(token.trim() ? "Token saved for this browser" : "Token removed", "success");
          }}
        >
          <label className="flex min-w-[16rem] flex-1 flex-col gap-1 text-sm">
            <span className="text-muted">Token</span>
            <input
              type="password"
              autoComplete="off"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              placeholder="atk_..."
              className="h-9 rounded-[var(--radius-control)] border border-line-strong bg-surface px-2.5 text-base text-ink"
            />
          </label>
          <Button type="submit">Save token</Button>
        </form>
      </Card>
    </Page>
  );
}
