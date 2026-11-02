import { Plus, Trash2 } from "lucide-react";
import { useState } from "react";

import { describeError } from "@/api/client";
import { TextField, useDebounced } from "@/components/forms";
import {
  Badge,
  Button,
  ConfirmDialog,
  ErrorState,
  IconButton,
  LoadingBlock,
  useToast,
} from "@/components/ui";
import { useQuery } from "@tanstack/react-query";

import { checkExecutable, useExecutables, useSetExecutables } from "./api";

export function ExecutablesPanel() {
  const allow = useExecutables();
  const setList = useSetExecutables();
  const toast = useToast();
  const [path, setPath] = useState("");
  const [removing, setRemoving] = useState<string | null>(null);
  const debounced = useDebounced(path.trim(), 300);
  const check = useQuery({
    queryKey: ["allowed-executables", "check", debounced] as const,
    queryFn: () => checkExecutable(debounced),
    enabled: debounced.length > 0,
  });

  if (allow.isPending) return <LoadingBlock label="Loading allowlist" />;
  if (allow.isError) return <ErrorState error={allow.error} onRetry={() => void allow.refetch()} />;
  const stored = allow.data.stored;

  const add = () => {
    setList.mutate([...stored, path.trim()], {
      onSuccess: () => {
        toast.show("Executable allowlisted", "success");
        setPath("");
      },
      onError: (e) => toast.show(describeError(e), "error"),
    });
  };

  return (
    <div className="flex flex-col gap-4">
      <p className="max-w-[72ch] text-sm text-muted">
        Process adapters only launch executables listed here (symlinks are resolved first), and
        check again every time a run starts. Claude Code and Codex CLI may also run{" "}
        <code>claude</code> and <code>codex</code> found on PATH. Operators can add entries with{" "}
        <code>{allow.data.env_var}</code>.
      </p>
      {allow.data.entries.length === 0 ? (
        <p className="text-sm text-faint">No executables are allowlisted yet.</p>
      ) : (
        <ul className="divide-y divide-line rounded-[var(--radius-panel)] border border-line bg-surface">
          {allow.data.entries.map((e) => (
            <li
              key={`${e.source}-${e.path}`}
              className="flex flex-wrap items-center gap-3 px-4 py-2.5"
            >
              <code className="min-w-0 flex-1 truncate" title={e.path}>
                {e.path}
              </code>
              <Badge tone={e.source === "env" ? "violet" : "neutral"}>
                {e.source === "env" ? "From environment" : "Added here"}
              </Badge>
              {e.exists ? null : <Badge tone="danger">Missing</Badge>}
              {e.source === "settings" ? (
                <IconButton
                  size="sm"
                  label={`Remove ${e.path}`}
                  icon={<Trash2 className="size-3.5" />}
                  onClick={() => setRemoving(e.path)}
                />
              ) : null}
            </li>
          ))}
        </ul>
      )}
      <form
        className="flex flex-col gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          add();
        }}
      >
        <div className="flex flex-wrap items-end gap-2">
          <TextField
            className="min-w-[18rem] flex-1"
            label="Add an executable"
            mono
            value={path}
            onChange={setPath}
            placeholder="/usr/bin/python3 or python3"
            autoComplete="off"
          />
          <Button
            type="submit"
            icon={<Plus className="size-4" aria-hidden />}
            loading={setList.isPending}
            disabled={!path.trim() || !check.data?.path}
          >
            Allowlist
          </Button>
        </div>
        {debounced && check.data ? (
          <p className="text-sm" aria-live="polite">
            {check.data.path ? (
              <>
                Resolves to <code>{check.data.path}</code>.{" "}
                {check.data.allowed ? (
                  <span className="text-[var(--st-succeeded)]">Already allowed.</span>
                ) : (
                  <span className="text-muted">Not allowed yet.</span>
                )}
              </>
            ) : (
              <span className="text-[var(--st-failed)]">{check.data.error}</span>
            )}
          </p>
        ) : null}
      </form>
      <ConfirmDialog
        open={removing !== null}
        title="Remove this executable?"
        description={
          <>
            Profiles that launch <code>{removing}</code> will be refused the next time they run.
          </>
        }
        confirmLabel="Remove"
        tone="danger"
        busy={setList.isPending}
        onClose={() => setRemoving(null)}
        onConfirm={() =>
          setList.mutate(
            stored.filter((s) => s !== removing),
            {
              onSuccess: () => {
                toast.show("Removed from the allowlist", "success");
                setRemoving(null);
              },
              onError: (e) => toast.show(describeError(e), "error"),
            },
          )
        }
      />
    </div>
  );
}
