import { Archive, ArchiveRestore, RefreshCw } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";

import { describeError } from "@/api/client";
import type { Run, RunStatus } from "@/api/types";
import { TagInput, TextAreaField, TextField } from "@/components/forms";
import { Page } from "@/components/layout/Page";
import {
  Badge,
  Button,
  Card,
  ConfirmDialog,
  Duration,
  ErrorState,
  LoadingBlock,
  RelativeTime,
  StatusPill,
  Tabs,
  useToast,
} from "@/components/ui";
import { cn } from "@/lib/cn";
import { humanize } from "@/lib/format";

import {
  useProject,
  useProjectActivity,
  useRefreshGit,
  useUpdateProject,
  type ActivitySection,
  type Project,
} from "./api";
import { GitStateView } from "./PathCheckView";
import { formToSettings, SettingsEditor, settingsToForm } from "./SettingsEditor";

const TABS = [
  { id: "activity", label: "Activity" },
  { id: "settings", label: "Settings" },
  { id: "git", label: "Git" },
];

function RunRows({ runs, empty }: { runs: Run[]; empty: string }) {
  if (runs.length === 0) return <p className="text-sm text-muted">{empty}</p>;
  return (
    <ul className="divide-y divide-line">
      {runs.map((r) => (
        <li key={r.id} className="flex flex-wrap items-center gap-3 py-2">
          <StatusPill status={r.status} size="sm" />
          <Link
            to={`/runs/${r.id}`}
            className="min-w-0 flex-1 truncate font-medium hover:text-accent"
          >
            {r.title}
          </Link>
          {r.failure_category ? (
            <span className="text-xs text-[var(--st-failed)]">{humanize(r.failure_category)}</span>
          ) : null}
          <span className="text-sm text-muted">
            {r.duration_ms !== null ? (
              <Duration ms={r.duration_ms} />
            ) : (
              <RelativeTime iso={r.created_at} />
            )}
          </span>
        </li>
      ))}
    </ul>
  );
}

function Section({
  title,
  section,
  render,
  empty,
  onRetry,
}: {
  title: string;
  section: ActivitySection;
  render: (item: Record<string, unknown>) => React.ReactNode;
  empty: string;
  onRetry: () => void;
}) {
  return (
    <Card
      title={title}
      description={
        section.available && section.total !== null ? `${section.total} in this project` : undefined
      }
    >
      {!section.available ? (
        <div className="flex flex-wrap items-center justify-between gap-3">
          <p className="text-sm text-muted" role="status">
            Could not be loaded{section.note ? `: ${section.note}` : ""}.
          </p>
          <Button size="sm" onClick={onRetry}>
            Try again
          </Button>
        </div>
      ) : section.items.length === 0 ? (
        <p className="text-sm text-muted">{empty}</p>
      ) : (
        <ul className="divide-y divide-line">
          {section.items.map((item, i) => (
            <li key={typeof item.id === "string" ? item.id : i} className="py-2 text-sm">
              {render(item)}
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function ActivityTab({ project }: { project: Project }) {
  const activity = useProjectActivity(project.id);
  if (activity.isPending) return <LoadingBlock label="Loading activity" />;
  if (activity.isError)
    return <ErrorState error={activity.error} onRetry={() => void activity.refetch()} />;
  const a = activity.data;
  const counts = Object.entries(a.status_counts).sort((x, y) => y[1] - x[1]);
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2" aria-label="Runs by status">
        <span className="text-sm text-muted">
          {a.runs_total} run{a.runs_total === 1 ? "" : "s"}
        </span>
        {counts.map(([status, n]) => (
          <span key={status} className="inline-flex items-center gap-1">
            <StatusPill status={status as RunStatus} size="sm" />
            <span className="text-sm tabular-nums">{n}</span>
          </span>
        ))}
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Recent runs">
          <RunRows runs={a.runs} empty="No runs in this project yet." />
        </Card>
        <Card title="Recent failures">
          <RunRows runs={a.recent_failures} empty="No failed or timed-out runs." />
        </Card>
        <Card title="Agent profiles" description="Profiles assigned to this project">
          {a.profiles.length === 0 ? (
            <p className="text-sm text-muted">None assigned. Any profile can still run here.</p>
          ) : (
            <ul className="divide-y divide-line">
              {a.profiles.map((p) => (
                <li key={p.id} className="flex items-center justify-between gap-2 py-2 text-sm">
                  <Link to={`/agents/${p.id}`} className="font-medium hover:text-accent">
                    {p.name}
                  </Link>
                  <span className="text-muted">
                    {p.adapter_type} v{p.latest_version}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Card>
        <Card title="Prompts" description="Prompts assigned to this project">
          {a.prompts.length === 0 ? (
            <p className="text-sm text-muted">None assigned.</p>
          ) : (
            <ul className="divide-y divide-line">
              {a.prompts.map((p) => (
                <li key={p.id} className="flex items-center justify-between gap-2 py-2 text-sm">
                  <Link to={`/prompts/${p.id}`} className="font-medium hover:text-accent">
                    {p.name}
                  </Link>
                  <span className="text-muted">v{p.latest_version}</span>
                </li>
              ))}
            </ul>
          )}
        </Card>
        <Section
          title="Schedules"
          section={a.schedules}
          empty="No schedules for this project."
          onRetry={() => void activity.refetch()}
          render={(s) => (
            <span className="flex items-center justify-between gap-2">
              <span className="font-medium">{String(s.name)}</span>
              <span className="text-muted">
                {s.enabled ? "Enabled" : "Paused"}
                {typeof s.next_run_at === "string" ? (
                  <>
                    , next <RelativeTime iso={s.next_run_at} />
                  </>
                ) : null}
              </span>
            </span>
          )}
        />
        <Section
          title="Workflows"
          section={a.workflows}
          empty="No workflows for this project."
          onRetry={() => void activity.refetch()}
          render={(w) => (
            <span className="flex items-center justify-between gap-2">
              <span className="font-medium">{String(w.name)}</span>
              <span className="text-muted">v{String(w.latest_version)}</span>
            </span>
          )}
        />
      </div>
    </div>
  );
}

function SettingsTab({ project }: { project: Project }) {
  const [name, setName] = useState(project.name);
  const [description, setDescription] = useState(project.description ?? "");
  const [branch, setBranch] = useState(project.default_branch ?? "");
  const [tags, setTags] = useState(project.tags);
  const [form, setForm] = useState(() => settingsToForm(project.settings));
  const update = useUpdateProject(project.id);
  const toast = useToast();
  const { settings, errors } = formToSettings(form, project.settings);
  return (
    <form
      className="flex flex-col gap-5"
      onSubmit={(e) => {
        e.preventDefault();
        update.mutate(
          {
            name: name.trim(),
            description: description.trim() || null,
            default_branch: branch.trim() || null,
            tags,
            settings,
          },
          {
            onSuccess: () => toast.show("Project settings saved", "success"),
            onError: (err) => toast.show(describeError(err), "error"),
          },
        );
      }}
    >
      <div className="grid gap-4 sm:grid-cols-2">
        <TextField label="Name" required value={name} onChange={setName} />
        <TextField label="Default branch" value={branch} onChange={setBranch} mono />
        <TextAreaField
          className="sm:col-span-2"
          label="Description"
          rows={2}
          value={description}
          onChange={setDescription}
        />
        <div className="sm:col-span-2">
          <TagInput label="Tags" tags={tags} onChange={setTags} />
        </div>
      </div>
      <div>
        <h3 className="mb-3 text-base font-semibold">Execution settings</h3>
        <SettingsEditor form={form} onChange={setForm} errors={errors} />
      </div>
      <div className="flex justify-end">
        <Button
          type="submit"
          variant="primary"
          loading={update.isPending}
          disabled={project.archived || !name.trim() || Object.keys(errors).length > 0}
        >
          Save settings
        </Button>
      </div>
    </form>
  );
}

export function ProjectDetailPage() {
  const { projectId = "" } = useParams();
  const project = useProject(projectId);
  const refresh = useRefreshGit(projectId);
  const update = useUpdateProject(projectId);
  const toast = useToast();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const [confirm, setConfirm] = useState(false);
  const tab = TABS.some((t) => t.id === params.get("tab"))
    ? (params.get("tab") ?? "activity")
    : "activity";

  if (project.isPending) {
    return (
      <Page title="Project">
        <LoadingBlock label="Loading project" />
      </Page>
    );
  }
  if (project.isError) {
    return (
      <Page title="Project">
        <ErrorState
          error={project.error}
          resource="project"
          backTo="/projects"
          backLabel="Back to projects"
          onRetry={() => void project.refetch()}
        />
      </Page>
    );
  }
  const p = project.data;
  const git = p.git_state;
  return (
    <Page
      breadcrumb={
        <Link to="/projects" className="hover:text-accent">
          Projects
        </Link>
      }
      title={
        <span className="inline-flex flex-wrap items-center gap-2">
          {p.name}
          {p.archived ? <Badge tone="warning">Archived</Badge> : null}
          {p.is_demo ? <Badge>Demo data</Badge> : null}
        </span>
      }
      description={
        <span className="flex flex-col gap-1">
          <code className="text-sm text-ink-soft">{p.repo_path}</code>
          {p.description ? <span>{p.description}</span> : null}
        </span>
      }
      actions={
        <>
          <Button
            icon={
              <RefreshCw
                className={cn("size-4", refresh.isPending && "animate-spin")}
                aria-hidden
              />
            }
            onClick={() =>
              refresh.mutate(undefined, {
                onSuccess: () => toast.show("Git state refreshed", "success"),
                onError: (e) => toast.show(describeError(e), "error"),
              })
            }
          >
            Refresh Git state
          </Button>
          {p.archived ? (
            <Button
              icon={<ArchiveRestore className="size-4" aria-hidden />}
              loading={update.isPending}
              onClick={() =>
                update.mutate(
                  { archived: false },
                  {
                    onSuccess: () => toast.show("Project restored", "success"),
                    onError: (e) => toast.show(describeError(e), "error"),
                  },
                )
              }
            >
              Restore
            </Button>
          ) : (
            <Button
              variant="danger"
              icon={<Archive className="size-4" aria-hidden />}
              onClick={() => setConfirm(true)}
            >
              Archive
            </Button>
          )}
        </>
      }
    >
      {git ? (
        <div className="flex flex-wrap items-center gap-2 text-sm text-muted">
          {git.is_repo ? (
            <>
              <span>
                On <strong className="text-ink">{git.branch ?? "detached HEAD"}</strong>
              </span>
              {git.dirty ? (
                <Badge tone="warning">{git.dirty_count} uncommitted</Badge>
              ) : (
                <Badge tone="success">Clean</Badge>
              )}
            </>
          ) : (
            <span>Not a Git repository</span>
          )}
          {p.git_checked_at ? (
            <span>
              checked <RelativeTime iso={p.git_checked_at} />
            </span>
          ) : null}
        </div>
      ) : null}
      <Card>
        <Tabs
          label="Project sections"
          items={TABS}
          value={tab}
          onChange={(id) =>
            setParams(
              (prev) => {
                const next = new URLSearchParams(prev);
                next.set("tab", id);
                return next;
              },
              { replace: true },
            )
          }
        >
          {tab === "activity" ? (
            <ActivityTab project={p} />
          ) : tab === "settings" ? (
            <SettingsTab key={p.updated_at} project={p} />
          ) : git ? (
            <GitStateView git={git} />
          ) : (
            <p className="text-sm text-muted">
              Git state has not been captured yet. Refresh it above.
            </p>
          )}
        </Tabs>
      </Card>
      <ConfirmDialog
        open={confirm}
        title="Archive this project?"
        description="Archived projects cannot start new runs and are hidden from lists. Nothing on disk is touched, past runs are kept, and you can restore it at any time."
        confirmLabel="Archive project"
        tone="danger"
        busy={update.isPending}
        onClose={() => setConfirm(false)}
        onConfirm={() =>
          update.mutate(
            { archived: true },
            {
              onSuccess: () => {
                setConfirm(false);
                toast.show("Project archived", "success");
                void navigate("/projects");
              },
              onError: (e) => toast.show(describeError(e), "error"),
            },
          )
        }
      />
    </Page>
  );
}
