/** Replay, Fork, and Compare controls in the run-detail header. */
import { Columns2, GitFork, Repeat } from "lucide-react";
import { useState } from "react";
import { useNavigate } from "react-router";

import { describeError } from "@/api/client";
import { TERMINAL_STATUSES, type RunDetail } from "@/api/types";
import type { RunDetailAction } from "@/app/registry";
import { Button, ConfirmDialog, useToast } from "@/components/ui";

import { compareTarget, useFork, useLineage, useReplay } from "./api";
import { DriftList, ForkDialog } from "./ForkDialog";

function LineageActions({ detail }: { detail: RunDetail }) {
  const { run } = detail;
  const [mode, setMode] = useState<"replay" | "fork" | null>(null);
  const lineage = useLineage(run.id, mode !== null);
  const replay = useReplay(run.id);
  const fork = useFork(run.id);
  const toast = useToast();
  const navigate = useNavigate();
  // Replay and Compare apply to agent and workflow runs; Fork changes agent-run settings
  // (prompt, agent profile, variables), so for workflow runs it explains why it is not offered.
  if (run.kind !== "agent" && run.kind !== "workflow") return null;
  const isWorkflow = run.kind === "workflow";
  const finished = TERMINAL_STATUSES.has(run.status);
  const options = lineage.data?.options;
  const drift = lineage.data?.drift ?? [];
  return (
    <>
      <Button
        icon={<Repeat className="size-4" aria-hidden />}
        disabled={!finished}
        title={
          finished
            ? "Run again with the same versions and inputs"
            : "Available once the run finishes"
        }
        onClick={() => setMode("replay")}
      >
        Replay
      </Button>
      <Button
        icon={<GitFork className="size-4" aria-hidden />}
        disabled={!finished}
        title={
          !finished
            ? "Available once the run finishes"
            : isWorkflow
              ? "Fork changes an agent run's prompt, profile, or variables; open to see why it does not apply here"
              : "Run again with changes"
        }
        onClick={() => setMode("fork")}
      >
        Fork
      </Button>
      <Button
        variant="ghost"
        icon={<Columns2 className="size-4" aria-hidden />}
        onClick={() => void navigate(compareTarget(detail))}
      >
        Compare
      </Button>
      <ConfirmDialog
        open={mode === "replay"}
        title="Replay this run?"
        description={
          <div className="flex flex-col gap-2">
            <p>
              {isWorkflow
                ? "A new workflow run starts with exactly the same workflow version and inputs. Every node runs again, and the new run is linked to this one as a replay."
                : "A new run starts with exactly the same prompt version, agent profile version, and inputs. It is linked to this run as a replay."}
            </p>
            {lineage.isPending ? <p className="text-muted">Checking the environment…</p> : null}
            {options && !options.replayable ? (
              <p className="text-[var(--st-failed)]">{options.reason}</p>
            ) : null}
            <DriftList drift={drift} />
          </div>
        }
        confirmLabel="Start replay"
        busy={replay.isPending}
        onClose={() => setMode(null)}
        onConfirm={() =>
          replay.mutate(undefined, {
            onSuccess: (r) => {
              setMode(null);
              toast.show("Replay started", "success");
              void navigate(`/runs/${r.result.run.id}`);
            },
            onError: (e) => toast.show(describeError(e), "error"),
          })
        }
      />
      {mode === "fork" && options ? (
        options.forkable ? (
          <ForkDialog
            open
            options={options}
            drift={drift}
            title={run.title}
            busy={fork.isPending}
            error={fork.error}
            onClose={() => {
              fork.reset();
              setMode(null);
            }}
            onSubmit={(body) =>
              fork.mutate(body, {
                onSuccess: (r) => {
                  setMode(null);
                  toast.show("Fork started", "success");
                  void navigate(`/runs/${r.result.run.id}`);
                },
              })
            }
          />
        ) : (
          <ConfirmDialog
            open
            title="This run cannot be forked"
            description={options.fork_reason ?? options.reason ?? undefined}
            confirmLabel="Close"
            onClose={() => setMode(null)}
            onConfirm={() => setMode(null)}
          />
        )
      ) : null}
    </>
  );
}

export const runDetailActions: RunDetailAction[] = [
  { id: "lineage", order: 10, render: ({ detail }) => <LineageActions detail={detail} /> },
];
