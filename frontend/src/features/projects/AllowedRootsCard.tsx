import { useState } from "react";

import { describeError } from "@/api/client";
import { ListEditor } from "@/components/forms";
import { Button, Card, ErrorState, LoadingBlock, useToast } from "@/components/ui";

import { useAllowedRoots, useSetAllowedRoots } from "./api";

/** Edit the project roots saved in Settings; shows the effective allowlist. */
export function AllowedRootsCard() {
  const roots = useAllowedRoots();
  const save = useSetAllowedRoots();
  const toast = useToast();
  const [draft, setDraft] = useState<string[] | null>(null);

  if (roots.isPending)
    return (
      <Card title="Allowed project roots">
        <LoadingBlock />
      </Card>
    );
  if (roots.isError)
    return (
      <Card title="Allowed project roots">
        <ErrorState error={roots.error} onRetry={() => void roots.refetch()} />
      </Card>
    );
  const values = draft ?? roots.data.roots;
  const saved = roots.data.roots;
  const extra = roots.data.effective.filter((r) => !saved.includes(r));
  return (
    <Card
      title="Allowed project roots"
      description="AgentTape only works inside these directories. Paths are canonicalized (symlinks and .. resolved) before the check."
    >
      <form
        className="flex flex-col gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate(values.map((v) => v.trim()).filter(Boolean), {
            onSuccess: () => {
              setDraft(null);
              toast.show("Allowed roots saved", "success");
            },
            onError: (err) => toast.show(describeError(err), "error"),
          });
        }}
      >
        <ListEditor
          label="Saved roots"
          itemLabel="Root"
          addLabel="Add root"
          values={values}
          onChange={setDraft}
          placeholder="/home/you/src"
          help="Absolute directories. The filesystem root itself is never allowed."
        />
        {extra.length ? (
          <div className="text-sm text-muted">
            Also allowed (from AGENTTAPE_ALLOWED_ROOTS or AgentTape's own fixtures):
            <ul className="mt-1">
              {extra.map((r) => (
                <li key={r}>
                  <code>{r}</code>
                </li>
              ))}
            </ul>
          </div>
        ) : null}
        <div className="flex justify-end gap-2">
          <Button disabled={draft === null} onClick={() => setDraft(null)}>
            Discard
          </Button>
          <Button
            type="submit"
            variant="primary"
            loading={save.isPending}
            disabled={draft === null}
          >
            Save roots
          </Button>
        </div>
      </form>
    </Card>
  );
}
