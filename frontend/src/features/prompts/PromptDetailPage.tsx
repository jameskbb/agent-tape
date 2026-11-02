import { Archive, ArchiveRestore, Copy, Download, Lock, Pencil, Play } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";

import { ApiError, describeError } from "@/api/client";
import { Modal, SelectField, TagInput, TextAreaField, TextField } from "@/components/forms";
import { Page } from "@/components/layout/Page";
import {
  Badge,
  Button,
  Card,
  CodeBlock,
  ConfirmDialog,
  EmptyState,
  ErrorState,
  LoadingBlock,
  RelativeTime,
  Tabs,
  useToast,
} from "@/components/ui";
import { cn } from "@/lib/cn";

import {
  downloadJson,
  exportPrompts,
  useDuplicatePrompt,
  usePrompt,
  usePromptDiff,
  usePromptVersions,
  useSaveVersion,
  useTestPrompt,
  useUpdatePrompt,
  type PromptTemplate,
  type PromptVersion,
  type VariableDefinition,
} from "./api";
import { DiffView } from "./DiffView";
import { serverIssueText } from "./PromptCreatePage";
import { RenderedPreview, useLivePreview } from "./RenderedPreview";
import { TemplateEditor } from "./TemplateEditor";
import { VariableInputs } from "./VariableInputs";
import {
  buildInputs,
  cleanVariables,
  definitionIssues,
  initialForm,
  type FormValue,
} from "./variables";

const TABS = [
  { id: "editor", label: "Editor" },
  { id: "preview", label: "Preview" },
  { id: "history", label: "History" },
  { id: "test", label: "Test run" },
];

function VersionSelect({
  versions,
  value,
  onChange,
  label = "Version",
}: {
  versions: PromptVersion[];
  value: string;
  onChange: (id: string) => void;
  label?: string;
}) {
  return (
    <SelectField
      label={label}
      value={value}
      onChange={onChange}
      className="max-w-[16rem]"
      options={versions.map((v) => ({
        value: v.id,
        label: `v${v.version}${v.locked_at ? " (used)" : ""}`,
      }))}
    />
  );
}

// ------------------------------------------------------------------------------ editor

function EditorTab({ template }: { template: PromptTemplate }) {
  const latest = template.latest;
  const [body, setBody] = useState(latest?.body ?? "");
  const [variables, setVariables] = useState<VariableDefinition[]>(latest?.variables ?? []);
  const [notes, setNotes] = useState("");
  const [error, setError] = useState<string | null>(null);
  const save = useSaveVersion(template.id);
  const toast = useToast();
  const issues = definitionIssues(body, variables);
  const dirty =
    body !== (latest?.body ?? "") ||
    JSON.stringify(cleanVariables(variables)) !==
      JSON.stringify(cleanVariables(latest?.variables ?? []));
  const next = template.latest_version + 1;

  const submit = () => {
    setError(null);
    save.mutate(
      { body, variables: cleanVariables(variables), notes: notes.trim() || null },
      {
        onSuccess: (v) => {
          setNotes("");
          toast.show(`Saved version ${v.version}`, "success");
        },
        onError: (e) => {
          if (e instanceof ApiError && e.status === 409) toast.show(e.message, "info");
          else setError(serverIssueText(e));
        },
      },
    );
  };

  if (template.archived) {
    return (
      <EmptyState
        title="This prompt is archived"
        description="Restore it to edit the template or save new versions. Its history stays intact."
      />
    );
  }

  return (
    <form
      className="flex flex-col gap-5"
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
    >
      <div
        className={cn(
          "flex items-start gap-2.5 rounded-[var(--radius-control)] border px-3 py-2.5 text-sm",
          latest?.locked_at
            ? "border-line bg-sunken text-ink-soft"
            : "border-[color-mix(in_srgb,var(--accent)_30%,transparent)] bg-accent-soft text-ink-soft",
        )}
      >
        {latest?.locked_at ? <Lock aria-hidden className="mt-0.5 size-4 shrink-0" /> : null}
        <p>
          {latest?.locked_at ? (
            <>
              Version {latest.version} was used by a run on <RelativeTime iso={latest.locked_at} />{" "}
              and is locked.{" "}
            </>
          ) : null}
          Saving your edits creates <strong>version {next}</strong>. Earlier versions never change,
          so past runs stay reproducible.
        </p>
      </div>
      <TemplateEditor
        body={body}
        variables={variables}
        onBody={setBody}
        onVariables={setVariables}
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
        <Button
          disabled={!dirty}
          onClick={() => {
            setBody(latest?.body ?? "");
            setVariables(latest?.variables ?? []);
          }}
        >
          Discard
        </Button>
        <Button
          type="submit"
          variant="primary"
          loading={save.isPending}
          disabled={!dirty || issues.length > 0 || !body.trim()}
        >
          Save as version {next}
        </Button>
      </div>
    </form>
  );
}

// ------------------------------------------------------------------------------ preview / test

function useVersionInputs(versions: PromptVersion[]) {
  const [versionId, setVersionId] = useState(versions[0]?.id ?? "");
  const version = versions.find((v) => v.id === versionId) ?? versions[0];
  const key = version?.id ?? "";
  // Form values are kept per version, so switching versions starts from that version's defaults.
  const [forms, setForms] = useState<Record<string, Record<string, FormValue>>>({});
  const form = forms[key] ?? initialForm(version?.variables ?? []);
  const setForm = (next: Record<string, FormValue>) => setForms({ ...forms, [key]: next });
  return { versionId: key, setVersionId, version, form, setForm };
}

function PreviewTab({
  template,
  versions,
}: {
  template: PromptTemplate;
  versions: PromptVersion[];
}) {
  const { versionId, setVersionId, version, form, setForm } = useVersionInputs(versions);
  const inputs = buildInputs(version?.variables ?? [], form);
  const preview = useLivePreview(template.id, versionId || null, inputs);
  return (
    <div className="grid gap-5 lg:grid-cols-2">
      <div className="flex flex-col gap-4">
        <VersionSelect versions={versions} value={versionId} onChange={setVersionId} />
        <VariableInputs
          definitions={version?.variables ?? []}
          values={form}
          onChange={setForm}
          issues={preview.data?.issues}
        />
      </div>
      <RenderedPreview preview={preview} />
    </div>
  );
}

function TestTab({ template, versions }: { template: PromptTemplate; versions: PromptVersion[] }) {
  const { versionId, setVersionId, version, form, setForm } = useVersionInputs(versions);
  const inputs = buildInputs(version?.variables ?? [], form);
  const preview = useLivePreview(template.id, versionId || null, inputs);
  const test = useTestPrompt(template.id);
  const navigate = useNavigate();
  const toast = useToast();
  return (
    <div className="flex flex-col gap-5">
      <p className="max-w-[72ch] text-sm text-muted">
        Runs this prompt with the built-in Demo Agent through the normal run engine. It needs no
        credentials and records a full timeline, so you can check the rendered prompt and the run it
        produces. Running a version locks it.
      </p>
      <div className="grid gap-5 lg:grid-cols-2">
        <div className="flex flex-col gap-4">
          <VersionSelect versions={versions} value={versionId} onChange={setVersionId} />
          <VariableInputs
            definitions={version?.variables ?? []}
            values={form}
            onChange={setForm}
            issues={preview.data?.issues}
          />
        </div>
        <RenderedPreview preview={preview} />
      </div>
      <div className="flex justify-end">
        <Button
          variant="primary"
          icon={<Play className="size-4" aria-hidden />}
          loading={test.isPending}
          disabled={template.archived || preview.data?.valid === false}
          onClick={() =>
            test.mutate(
              { inputs, version_id: versionId || null },
              {
                onSuccess: (r) => {
                  toast.show(`Started with ${r.profile.name}`, "success");
                  void navigate(`/runs/${r.run.id}`);
                },
                onError: (e) => toast.show(serverIssueText(e), "error"),
              },
            )
          }
        >
          Test with Demo Agent
        </Button>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------------------ history

function HistoryTab({
  template,
  versions,
}: {
  template: PromptTemplate;
  versions: PromptVersion[];
}) {
  const newest = versions[0]?.version ?? 1;
  const [from, setFrom] = useState(Math.max(1, newest - 1));
  const [to, setTo] = useState(newest);
  const [open, setOpen] = useState<string | null>(null);
  const canDiff = versions.length > 1 && from !== to;
  const diff = usePromptDiff(template.id, from, to, canDiff);
  const numbers = versions.map((v) => ({ value: String(v.version), label: `v${v.version}` }));
  return (
    <div className="grid gap-6 lg:grid-cols-[minmax(0,22rem)_minmax(0,1fr)]">
      <section aria-label="Versions">
        <ol className="flex flex-col divide-y divide-line rounded-[var(--radius-panel)] border border-line">
          {versions.map((v) => (
            <li key={v.id} className="flex flex-col gap-1 px-3 py-2.5">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-semibold tabular-nums">v{v.version}</span>
                {v.locked_at ? (
                  <Badge title="A run used this version, so it can never change">
                    <Lock aria-hidden className="size-3" /> Used by a run
                  </Badge>
                ) : (
                  <Badge tone="accent">Unused</Badge>
                )}
                <span className="ml-auto text-xs text-muted">
                  <RelativeTime iso={v.created_at} />
                </span>
              </div>
              {v.notes ? <p className="text-sm text-ink-soft">{v.notes}</p> : null}
              <p className="text-xs text-muted">
                {v.variables.length} variable{v.variables.length === 1 ? "" : "s"}
                {v.created_by ? `, by ${v.created_by}` : ""}
              </p>
              <button
                type="button"
                aria-expanded={open === v.id}
                onClick={() => setOpen(open === v.id ? null : v.id)}
                className="self-start text-sm text-accent hover:underline"
              >
                {open === v.id ? "Hide template" : "Show template"}
              </button>
              {open === v.id ? <CodeBlock code={v.body} maxHeight="16rem" /> : null}
            </li>
          ))}
        </ol>
      </section>
      <section aria-label="Compare versions" className="flex flex-col gap-3">
        {versions.length < 2 ? (
          <EmptyState
            title="Only one version so far"
            description="Save an edit in the Editor tab to create version 2, then compare the two here."
          />
        ) : (
          <>
            <div className="flex flex-wrap items-end gap-3">
              <SelectField
                label="Compare"
                value={String(from)}
                onChange={(v) => setFrom(Number(v))}
                options={numbers}
                className="w-32"
              />
              <SelectField
                label="With"
                value={String(to)}
                onChange={(v) => setTo(Number(v))}
                options={numbers}
                className="w-32"
              />
            </div>
            {!canDiff ? (
              <p className="text-sm text-muted">Pick two different versions.</p>
            ) : diff.isPending ? (
              <LoadingBlock label="Loading diff" />
            ) : diff.isError ? (
              <ErrorState error={diff.error} onRetry={() => void diff.refetch()} />
            ) : (
              <DiffView diff={diff.data} />
            )}
          </>
        )}
      </section>
    </div>
  );
}

// ------------------------------------------------------------------------------ page

function DetailsDialog({
  template,
  open,
  onClose,
}: {
  template: PromptTemplate;
  open: boolean;
  onClose: () => void;
}) {
  const [name, setName] = useState(template.name);
  const [description, setDescription] = useState(template.description ?? "");
  const [owner, setOwner] = useState(template.owner ?? "");
  const [tags, setTags] = useState(template.tags);
  const update = useUpdatePrompt(template.id);
  const toast = useToast();
  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Edit details"
      description="Details are not versioned; the template and variables are."
    >
      <form
        className="flex flex-col gap-4"
        onSubmit={(e) => {
          e.preventDefault();
          update.mutate(
            {
              name: name.trim(),
              description: description.trim() || null,
              owner: owner.trim() || null,
              tags,
            },
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
        <TextField label="Owner" value={owner} onChange={setOwner} />
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

export function PromptDetailPage() {
  const { promptId = "" } = useParams();
  const template = usePrompt(promptId);
  const versions = usePromptVersions(promptId);
  const [params, setParams] = useSearchParams();
  const tab = TABS.some((t) => t.id === params.get("tab"))
    ? (params.get("tab") ?? "editor")
    : "editor";
  const [confirmArchive, setConfirmArchive] = useState(false);
  const [editing, setEditing] = useState(false);
  const update = useUpdatePrompt(promptId);
  const duplicate = useDuplicatePrompt(promptId);
  const toast = useToast();
  const navigate = useNavigate();

  if (template.isPending || versions.isPending) {
    return (
      <Page title="Prompt">
        <LoadingBlock label="Loading prompt" />
      </Page>
    );
  }
  if (template.isError || versions.isError) {
    return (
      <Page title="Prompt">
        <ErrorState
          error={template.error ?? versions.error}
          resource="prompt"
          backTo="/prompts"
          backLabel="Back to prompts"
          onRetry={() => {
            void template.refetch();
            void versions.refetch();
          }}
        />
      </Page>
    );
  }
  const t = template.data;
  const archived = t.archived;

  const doExport = async () => {
    try {
      const bundle = await exportPrompts([t.id]);
      const slug = t.name
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, "-")
        .replace(/^-|-$/g, "");
      downloadJson(bundle, `prompt-${slug || t.id.slice(0, 8)}.json`);
    } catch (e) {
      toast.show(describeError(e), "error");
    }
  };

  return (
    <Page
      breadcrumb={
        <Link to="/prompts" className="hover:text-accent">
          Prompts
        </Link>
      }
      title={
        <span className="inline-flex flex-wrap items-center gap-2">
          {t.name}
          <Badge tone="accent">v{t.latest_version}</Badge>
          {archived ? <Badge tone="warning">Archived</Badge> : null}
          {t.is_demo ? <Badge>Demo data</Badge> : null}
        </span>
      }
      description={
        <span className="flex flex-col gap-1">
          {t.description ? <span>{t.description}</span> : null}
          <span className="flex flex-wrap items-center gap-1.5 text-sm">
            {t.owner ? <span>Owner: {t.owner}</span> : null}
            {t.tags.map((tag) => (
              <Badge key={tag}>{tag}</Badge>
            ))}
          </span>
        </span>
      }
      actions={
        <>
          <Button icon={<Pencil className="size-4" aria-hidden />} onClick={() => setEditing(true)}>
            Edit details
          </Button>
          <Button
            icon={<Copy className="size-4" aria-hidden />}
            loading={duplicate.isPending}
            onClick={() =>
              duplicate.mutate(
                {},
                {
                  onSuccess: (copy) => {
                    toast.show(`Created "${copy.name}"`, "success");
                    void navigate(`/prompts/${copy.id}`);
                  },
                  onError: (e) => toast.show(describeError(e), "error"),
                },
              )
            }
          >
            Duplicate
          </Button>
          <Button
            icon={<Download className="size-4" aria-hidden />}
            onClick={() => void doExport()}
          >
            Export
          </Button>
          {archived ? (
            <Button
              icon={<ArchiveRestore className="size-4" aria-hidden />}
              loading={update.isPending}
              onClick={() =>
                update.mutate(
                  { archived: false },
                  {
                    onSuccess: () => toast.show("Prompt restored", "success"),
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
              onClick={() => setConfirmArchive(true)}
            >
              Archive
            </Button>
          )}
        </>
      }
    >
      <Card padded>
        <Tabs
          label="Prompt sections"
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
          {tab === "editor" ? (
            <EditorTab key={`${t.id}-${t.latest_version}`} template={t} />
          ) : tab === "preview" ? (
            <PreviewTab template={t} versions={versions.data} />
          ) : tab === "history" ? (
            <HistoryTab key={t.latest_version} template={t} versions={versions.data} />
          ) : (
            <TestTab template={t} versions={versions.data} />
          )}
        </Tabs>
      </Card>
      <DetailsDialog
        key={t.updated_at}
        template={t}
        open={editing}
        onClose={() => setEditing(false)}
      />
      <ConfirmDialog
        open={confirmArchive}
        title="Archive this prompt?"
        description="Archived prompts are hidden from lists and cannot start new runs or get new versions. Past runs and every version are kept, and you can restore it at any time."
        confirmLabel="Archive prompt"
        tone="danger"
        busy={update.isPending}
        onClose={() => setConfirmArchive(false)}
        onConfirm={() =>
          update.mutate(
            { archived: true },
            {
              onSuccess: () => {
                setConfirmArchive(false);
                toast.show("Prompt archived", "success");
              },
              onError: (e) => toast.show(describeError(e), "error"),
            },
          )
        }
      />
    </Page>
  );
}
