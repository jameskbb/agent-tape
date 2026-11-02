import { Archive, ArchiveRestore, Lock, Pencil } from "lucide-react";
import { useState } from "react";
import { Link, useParams, useSearchParams } from "react-router";

import { describeError } from "@/api/client";
import { Modal, TagInput, TextAreaField, TextField } from "@/components/forms";
import { Page } from "@/components/layout/Page";
import {
  Badge,
  Button,
  Card,
  ConfirmDialog,
  EmptyState,
  ErrorState,
  JsonInspector,
  LoadingBlock,
  RelativeTime,
  Tabs,
  useToast,
} from "@/components/ui";
import { cn } from "@/lib/cn";

import { AdapterStatusBadge } from "./AdapterPanel";
import {
  useAdapterDetails,
  useCreateProfileVersion,
  useProfile,
  useProfileVersions,
  useUpdateProfile,
  type AgentProfile,
  type ProfileConfig,
  type ProfileVersion,
} from "./api";
import { ConfigEditor } from "./ConfigEditor";
import { configChanges } from "./configForm";
import { issuesFromError } from "./ProfileCreatePage";
import { useLiveValidation, ValidationSummary } from "./ValidationSummary";

const TABS = [
  { id: "config", label: "Configuration" },
  { id: "history", label: "History" },
];

function ConfigTab({ profile }: { profile: AgentProfile }) {
  const latestConfig = profile.latest?.config ?? {};
  const [config, setConfig] = useState<ProfileConfig>(latestConfig);
  const [notes, setNotes] = useState("");
  const [error, setError] = useState<string | null>(null);
  const validation = useLiveValidation(profile.adapter_type, config);
  const save = useCreateProfileVersion(profile.id);
  const toast = useToast();
  const issues = validation.data?.issues ?? [];
  const blocked = issues.some((i) => i.severity === "error");
  const dirty = JSON.stringify(config) !== JSON.stringify(latestConfig);
  const next = profile.latest_version + 1;
  if (profile.archived) {
    return (
      <EmptyState
        title="This profile is archived"
        description="Restore it to change its configuration or start runs with it. Its versions are kept."
      />
    );
  }
  return (
    <form
      className="flex flex-col gap-4"
      onSubmit={(e) => {
        e.preventDefault();
        setError(null);
        save.mutate(
          { config, notes: notes.trim() || null },
          {
            onSuccess: (v) => {
              setNotes("");
              toast.show(`Saved version ${v.version}`, "success");
            },
            onError: (err) => {
              const found = issuesFromError(err);
              setError(
                found.length
                  ? `${describeError(err)}: ${found.map((i) => `${i.field} ${i.message}`).join("; ")}`
                  : describeError(err),
              );
            },
          },
        );
      }}
    >
      <p className="rounded-[var(--radius-control)] border border-line bg-sunken px-3 py-2 text-sm text-ink-soft">
        Saving creates <strong>version {next}</strong>. Runs keep a snapshot of the version they
        used, so changing the profile never rewrites an earlier run.
      </p>
      <ValidationSummary query={validation} />
      <ConfigEditor
        adapterType={profile.adapter_type}
        config={config}
        onChange={setConfig}
        issues={issues}
      />
      <TextField
        label="Version note"
        value={notes}
        onChange={setNotes}
        placeholder="What changed and why (optional)"
      />
      {error ? (
        <p role="alert" className="text-sm text-[var(--st-failed)]">
          {error}
        </p>
      ) : null}
      <div className="flex flex-wrap items-center justify-end gap-2">
        {dirty ? <span className="text-sm text-muted">Unsaved changes</span> : null}
        <Button disabled={!dirty} onClick={() => setConfig(latestConfig)}>
          Discard
        </Button>
        <Button
          type="submit"
          variant="primary"
          loading={save.isPending}
          disabled={!dirty || blocked}
        >
          Save as version {next}
        </Button>
      </div>
    </form>
  );
}

function renderValue(v: unknown) {
  if (v === undefined) return <span className="text-faint italic">not set</span>;
  return <code className="break-all">{JSON.stringify(v)}</code>;
}

function HistoryTab({ versions }: { versions: ProfileVersion[] }) {
  const [selected, setSelected] = useState(versions[0]?.id ?? "");
  const index = versions.findIndex((v) => v.id === selected);
  const version = versions[index];
  const previous = versions[index + 1];
  const changes = version && previous ? configChanges(previous.config, version.config) : [];
  return (
    <div className="grid gap-6 lg:grid-cols-[minmax(0,20rem)_minmax(0,1fr)]">
      <ol
        className="flex flex-col divide-y divide-line rounded-[var(--radius-panel)] border border-line"
        aria-label="Versions"
      >
        {versions.map((v) => (
          <li key={v.id}>
            <button
              type="button"
              aria-current={v.id === selected}
              onClick={() => setSelected(v.id)}
              className={cn(
                "flex w-full flex-col gap-1 px-3 py-2.5 text-left",
                v.id === selected ? "bg-accent-soft" : "hover:bg-sunken",
              )}
            >
              <span className="flex flex-wrap items-center gap-2">
                <span className="font-semibold tabular-nums">v{v.version}</span>
                {v.locked_at ? (
                  <Badge>
                    <Lock aria-hidden className="size-3" /> Used by a run
                  </Badge>
                ) : null}
                <span className="ml-auto text-xs text-muted">
                  <RelativeTime iso={v.created_at} />
                </span>
              </span>
              {v.notes ? <span className="text-sm text-ink-soft">{v.notes}</span> : null}
            </button>
          </li>
        ))}
      </ol>
      {version ? (
        <div className="flex flex-col gap-4">
          <section aria-label="Changes">
            <h3 className="mb-2 text-sm font-semibold">
              {previous
                ? `Changes from v${previous.version} to v${version.version}`
                : "First version"}
            </h3>
            {previous ? (
              changes.length === 0 ? (
                <p className="text-sm text-muted">Configuration unchanged.</p>
              ) : (
                <table className="w-full text-left text-sm">
                  <caption className="sr-only">Changed settings</caption>
                  <thead className="text-muted">
                    <tr>
                      <th scope="col" className="py-1 pr-3 font-medium">
                        Setting
                      </th>
                      <th scope="col" className="py-1 pr-3 font-medium">
                        Before
                      </th>
                      <th scope="col" className="py-1 font-medium">
                        After
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {changes.map((c) => (
                      <tr key={c.key} className="border-t border-line align-top">
                        <td className="py-1.5 pr-3 font-mono">{c.key}</td>
                        <td className="bg-[var(--st-failed-bg)] px-2 py-1.5">
                          {renderValue(c.before)}
                        </td>
                        <td className="bg-[var(--st-succeeded-bg)] px-2 py-1.5">
                          {renderValue(c.after)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )
            ) : null}
          </section>
          <section aria-label="Full configuration">
            <h3 className="mb-2 text-sm font-semibold">Configuration of v{version.version}</h3>
            <JsonInspector value={version.config} defaultDepth={2} />
          </section>
        </div>
      ) : null}
    </div>
  );
}

function DetailsDialog({
  profile,
  open,
  onClose,
}: {
  profile: AgentProfile;
  open: boolean;
  onClose: () => void;
}) {
  const [name, setName] = useState(profile.name);
  const [description, setDescription] = useState(profile.description ?? "");
  const [tags, setTags] = useState(profile.tags);
  const update = useUpdateProfile(profile.id);
  const toast = useToast();
  return (
    <Modal open={open} onClose={onClose} title="Edit details">
      <form
        className="flex flex-col gap-4"
        onSubmit={(e) => {
          e.preventDefault();
          update.mutate(
            { name: name.trim(), description: description.trim() || null, tags },
            {
              onSuccess: () => {
                toast.show("Details saved", "success");
                onClose();
              },
              onError: (err) => toast.show(describeError(err), "error"),
            },
          );
        }}
      >
        <TextField label="Name" required value={name} onChange={setName} />
        <TextAreaField label="Description" rows={2} value={description} onChange={setDescription} />
        <TagInput label="Tags" tags={tags} onChange={setTags} />
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button
            type="submit"
            variant="primary"
            loading={update.isPending}
            disabled={!name.trim()}
          >
            Save details
          </Button>
        </div>
      </form>
    </Modal>
  );
}

export function ProfileDetailPage() {
  const { profileId = "" } = useParams();
  const profile = useProfile(profileId);
  const versions = useProfileVersions(profileId);
  const adapters = useAdapterDetails();
  const update = useUpdateProfile(profileId);
  const toast = useToast();
  const [params, setParams] = useSearchParams();
  const [confirm, setConfirm] = useState(false);
  const [editing, setEditing] = useState(false);
  const tab = params.get("tab") === "history" ? "history" : "config";

  if (profile.isPending || versions.isPending) {
    return (
      <Page title="Agent profile">
        <LoadingBlock label="Loading profile" />
      </Page>
    );
  }
  if (profile.isError || versions.isError) {
    return (
      <Page title="Agent profile">
        <ErrorState
          error={profile.error ?? versions.error}
          resource="agent profile"
          backTo="/agents"
          backLabel="Back to agents"
          onRetry={() => void profile.refetch()}
        />
      </Page>
    );
  }
  const p = profile.data;
  const adapter = adapters.data?.find((a) => a.name === p.adapter_type);
  return (
    <Page
      breadcrumb={
        <Link to="/agents" className="hover:text-accent">
          Agents
        </Link>
      }
      title={
        <span className="inline-flex flex-wrap items-center gap-2">
          {p.name}
          <Badge tone="accent">v{p.latest_version}</Badge>
          {p.archived ? <Badge tone="warning">Archived</Badge> : null}
          {p.is_demo ? <Badge>Demo data</Badge> : null}
        </span>
      }
      description={
        <span className="flex flex-col gap-1">
          {p.description ? <span>{p.description}</span> : null}
          <span className="flex flex-wrap items-center gap-1.5 text-sm">
            <span>{adapter?.display_name ?? p.adapter_type}</span>
            {adapter ? <AdapterStatusBadge status={adapter.status} /> : null}
            {p.tags.map((t) => (
              <Badge key={t}>{t}</Badge>
            ))}
          </span>
        </span>
      }
      actions={
        <>
          <Button icon={<Pencil className="size-4" aria-hidden />} onClick={() => setEditing(true)}>
            Edit details
          </Button>
          {p.archived ? (
            <Button
              icon={<ArchiveRestore className="size-4" aria-hidden />}
              loading={update.isPending}
              onClick={() =>
                update.mutate(
                  { archived: false },
                  {
                    onSuccess: () => toast.show("Profile restored", "success"),
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
      <Card>
        <Tabs
          label="Profile sections"
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
          {tab === "config" ? (
            <ConfigTab key={`${p.id}-${p.latest_version}`} profile={p} />
          ) : (
            <HistoryTab key={p.latest_version} versions={versions.data} />
          )}
        </Tabs>
      </Card>
      <DetailsDialog
        key={p.updated_at}
        profile={p}
        open={editing}
        onClose={() => setEditing(false)}
      />
      <ConfirmDialog
        open={confirm}
        title="Archive this profile?"
        description="Archived profiles cannot start new runs and are hidden from lists. Past runs keep their snapshot, and you can restore the profile at any time."
        confirmLabel="Archive profile"
        tone="danger"
        busy={update.isPending}
        onClose={() => setConfirm(false)}
        onConfirm={() =>
          update.mutate(
            { archived: true },
            {
              onSuccess: () => {
                setConfirm(false);
                toast.show("Profile archived", "success");
              },
              onError: (e) => toast.show(describeError(e), "error"),
            },
          )
        }
      />
    </Page>
  );
}
