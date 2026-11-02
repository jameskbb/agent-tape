import { useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";

import { ApiError, describeError } from "@/api/client";
import { TagInput, TextAreaField, TextField } from "@/components/forms";
import { Page } from "@/components/layout/Page";
import { Button, Card, ErrorState, LoadingBlock, useToast } from "@/components/ui";

import { AdapterCard } from "./AdapterPanel";
import {
  useAdapterDetails,
  useCreateProfile,
  type ProfileConfig,
  type ValidationIssue,
} from "./api";
import { ConfigEditor } from "./ConfigEditor";
import { useLiveValidation, ValidationSummary } from "./ValidationSummary";

export const DEFAULT_CONFIGS: Record<string, ProfileConfig> = {
  demo: { scenario: "success" },
  "generic-command": { prompt_mode: "stdin", parser: "text" },
  "claude-code": { permission_mode: "default", max_turns: 20 },
  "codex-cli": { sandbox: "workspace-write" },
  herdr: { agent_kind: "demo", workspace_policy: "new", cleanup: "keep", fallback: "none" },
};

/** Issues attached to a failed save (422 with details.issues). */
export function issuesFromError(error: unknown): ValidationIssue[] {
  if (error instanceof ApiError && Array.isArray(error.details.issues)) {
    return error.details.issues as ValidationIssue[];
  }
  return [];
}

function ProfileForm({ adapterType }: { adapterType: string }) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [tags, setTags] = useState<string[]>([]);
  const [config, setConfig] = useState<ProfileConfig>(DEFAULT_CONFIGS[adapterType] ?? {});
  const [error, setError] = useState<string | null>(null);
  const validation = useLiveValidation(adapterType, config);
  const create = useCreateProfile();
  const navigate = useNavigate();
  const toast = useToast();
  const issues = validation.data?.issues ?? [];
  const blocked = issues.some((i) => i.severity === "error");

  return (
    <form
      className="flex flex-col gap-5"
      onSubmit={(e) => {
        e.preventDefault();
        setError(null);
        create.mutate(
          {
            name: name.trim(),
            adapter_type: adapterType,
            description: description.trim() || null,
            tags,
            config,
            notes: "First version",
          },
          {
            onSuccess: (p) => {
              toast.show("Profile saved", "success");
              void navigate(`/agents/${p.id}`);
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
      <Card title="Details">
        <div className="grid gap-4 sm:grid-cols-2">
          <TextField label="Name" required value={name} onChange={setName} maxLength={200} />
          <TagInput label="Tags" tags={tags} onChange={setTags} />
          <TextAreaField
            className="sm:col-span-2"
            label="Description"
            rows={2}
            value={description}
            onChange={setDescription}
          />
        </div>
      </Card>
      <Card
        title="Configuration"
        description="Checked against this machine as you type: the executable must exist and be allowlisted, arguments are an array, and limits must be valid."
      >
        <div className="flex flex-col gap-4">
          <ValidationSummary query={validation} />
          <ConfigEditor
            adapterType={adapterType}
            config={config}
            onChange={setConfig}
            issues={issues}
          />
        </div>
      </Card>
      {error ? (
        <p role="alert" className="text-sm text-[var(--st-failed)]">
          {error}
        </p>
      ) : null}
      <div className="flex justify-end gap-2">
        <Button onClick={() => void navigate("/agents")}>Cancel</Button>
        <Button
          type="submit"
          variant="primary"
          loading={create.isPending}
          disabled={!name.trim() || blocked}
        >
          Save profile
        </Button>
      </div>
    </form>
  );
}

export function ProfileCreatePage() {
  const adapters = useAdapterDetails();
  const [params, setParams] = useSearchParams();
  const selected = params.get("adapter");
  return (
    <Page
      title="New agent profile"
      breadcrumb={
        <Link to="/agents" className="hover:text-accent">
          Agents
        </Link>
      }
      description="Pick an adapter, then describe how it should run. The profile is validated before it is saved."
    >
      {adapters.isPending ? (
        <LoadingBlock label="Discovering adapters" />
      ) : adapters.isError ? (
        <ErrorState error={adapters.error} onRetry={() => void adapters.refetch()} />
      ) : (
        <>
          <section aria-label="Adapter" className="grid gap-3 md:grid-cols-2">
            {adapters.data.map((a) => (
              <AdapterCard
                key={a.name}
                adapter={a}
                selected={selected === a.name}
                onSelect={() =>
                  setParams(
                    (prev) => {
                      const next = new URLSearchParams(prev);
                      next.set("adapter", a.name);
                      return next;
                    },
                    { replace: true },
                  )
                }
              />
            ))}
          </section>
          {selected ? (
            <ProfileForm key={selected} adapterType={selected} />
          ) : (
            <p className="text-sm text-muted">Choose an adapter above to continue.</p>
          )}
        </>
      )}
    </Page>
  );
}
