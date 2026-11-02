import { KeyRound, Pencil, Plus, Trash2 } from "lucide-react";
import { useState } from "react";

import { describeError } from "@/api/client";
import { Modal, TextField } from "@/components/forms";
import {
  Badge,
  Button,
  ConfirmDialog,
  EmptyState,
  ErrorState,
  IconButton,
  LoadingBlock,
  RelativeTime,
  useToast,
} from "@/components/ui";

import { useDeleteSecret, useSaveSecret, useSecrets, type SecretReference } from "./api";

function SecretDialog({
  secret,
  open,
  onClose,
}: {
  secret: SecretReference | null;
  open: boolean;
  onClose: () => void;
}) {
  const [name, setName] = useState(secret?.name ?? "");
  const [locator, setLocator] = useState(secret?.locator ?? "");
  const [description, setDescription] = useState(secret?.description ?? "");
  const [error, setError] = useState<string | null>(null);
  const save = useSaveSecret();
  const toast = useToast();
  const nameOk = /^[A-Za-z_][A-Za-z0-9_]{0,127}$/.test(name);
  return (
    <Modal
      open={open}
      onClose={onClose}
      title={secret ? `Edit ${secret.name}` : "Add a secret reference"}
      description="A secret reference names where a value lives. AgentTape stores only the name and the environment variable to read, never the value."
    >
      <form
        className="flex flex-col gap-4"
        onSubmit={(e) => {
          e.preventDefault();
          setError(null);
          save.mutate(
            {
              id: secret?.id,
              body: secret
                ? { locator: locator.trim() || null, description: description.trim() || null }
                : {
                    name,
                    locator: locator.trim() || null,
                    description: description.trim() || null,
                  },
            },
            {
              onSuccess: () => {
                toast.show(secret ? "Secret reference saved" : "Secret reference added", "success");
                onClose();
              },
              onError: (err) => setError(describeError(err)),
            },
          );
        }}
      >
        <TextField
          label="Name"
          mono
          required
          disabled={Boolean(secret)}
          value={name}
          onChange={setName}
          error={name && !nameOk ? "Use letters, digits, and _" : null}
          help="Used in prompts as {{ secret:NAME }} and in profiles' secret environment."
          autoComplete="off"
        />
        <TextField
          label="Environment variable"
          mono
          value={locator}
          onChange={setLocator}
          placeholder={name || "Defaults to the name"}
          help="Read from the AgentTape server's environment when a run starts."
          autoComplete="off"
        />
        <TextField label="Description" value={description} onChange={setDescription} />
        {error ? (
          <p role="alert" className="text-sm text-[var(--st-failed)]">
            {error}
          </p>
        ) : null}
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button type="submit" variant="primary" loading={save.isPending} disabled={!nameOk}>
            {secret ? "Save" : "Add reference"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

export function SecretsPanel() {
  const secrets = useSecrets();
  const remove = useDeleteSecret();
  const toast = useToast();
  const [editing, setEditing] = useState<SecretReference | "new" | null>(null);
  const [deleting, setDeleting] = useState<SecretReference | null>(null);

  if (secrets.isPending) return <LoadingBlock label="Loading secret references" />;
  if (secrets.isError)
    return <ErrorState error={secrets.error} onRetry={() => void secrets.refetch()} />;

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="max-w-[72ch] text-sm text-muted">
          Values are never stored or displayed. Each reference is resolved from the server's
          environment only when a run starts, and the value is redacted from events and logs.
        </p>
        <Button icon={<Plus className="size-4" aria-hidden />} onClick={() => setEditing("new")}>
          Add reference
        </Button>
      </div>
      {secrets.data.length === 0 ? (
        <EmptyState
          icon={<KeyRound className="size-6" />}
          title="No secret references"
          description="Add one when a prompt or profile needs a credential, such as a GitHub token. Without a reference, AgentTape reads the environment variable with the same name."
        />
      ) : (
        <ul className="divide-y divide-line rounded-[var(--radius-panel)] border border-line bg-surface">
          {secrets.data.map((s) => (
            <li key={s.id} className="flex flex-wrap items-center gap-3 px-4 py-3">
              <div className="min-w-0 flex-1">
                <p className="font-mono text-sm font-medium">{s.name}</p>
                <p className="text-xs text-muted">
                  Reads <code>${s.locator}</code>
                  {s.description ? `. ${s.description}` : ""}
                </p>
              </div>
              {s.available ? (
                <Badge tone="success">Set in environment</Badge>
              ) : (
                <Badge tone="warning">Not set in environment</Badge>
              )}
              <span className="text-xs text-muted">
                Added <RelativeTime iso={s.created_at} />
              </span>
              <IconButton
                size="sm"
                label={`Edit ${s.name}`}
                icon={<Pencil className="size-3.5" />}
                onClick={() => setEditing(s)}
              />
              <IconButton
                size="sm"
                label={`Delete ${s.name}`}
                icon={<Trash2 className="size-3.5" />}
                onClick={() => setDeleting(s)}
              />
            </li>
          ))}
        </ul>
      )}
      <SecretDialog
        key={editing === "new" ? "new" : (editing?.id ?? "none")}
        secret={editing === "new" ? null : editing}
        open={editing !== null}
        onClose={() => setEditing(null)}
      />
      <ConfirmDialog
        open={deleting !== null}
        title={`Delete ${deleting?.name ?? "this reference"}?`}
        description="Prompts and profiles that use this name will fall back to the environment variable with the same name. Past runs are not affected."
        confirmLabel="Delete reference"
        tone="danger"
        busy={remove.isPending}
        onClose={() => setDeleting(null)}
        onConfirm={() => {
          if (!deleting) return;
          remove.mutate(deleting.id, {
            onSuccess: () => {
              toast.show("Secret reference deleted", "success");
              setDeleting(null);
            },
            onError: (e) => toast.show(describeError(e), "error"),
          });
        }}
      />
    </div>
  );
}
