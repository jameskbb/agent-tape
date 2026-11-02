import { RotateCcw } from "lucide-react";
import { useId, useState } from "react";

import { describeError } from "@/api/client";
import { TERMINAL_STATUSES, type EvaluationResult, type RunDetail } from "@/api/types";
import { Badge, Button, Card, EmptyState, JsonInspector, useToast } from "@/components/ui";
import { humanize } from "@/lib/format";

import { useEvaluationSuites, useEvaluateRun, useHumanScore, useRerunEvaluations } from "./api";

const TONE = {
  pass: "success",
  fail: "danger",
  warning: "warning",
  error: "danger",
  skipped: "neutral",
} as const;

function ResultItem({ result, node }: { result: EvaluationResult; node?: string }) {
  const rerunOf = result.evidence.rerun_of;
  const human = result.evidence.source === "human";
  return (
    <li className="rounded-[var(--radius-panel)] border border-line bg-surface p-4">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={TONE[result.status]}>{humanize(result.status)}</Badge>
        <span className="font-medium">{result.name}</span>
        {result.required ? <Badge tone="accent">Required gate</Badge> : null}
        {node ? <Badge>Node {node}</Badge> : null}
        {human ? <Badge tone="violet">Human</Badge> : null}
        {typeof rerunOf === "string" ? <Badge>Rerun</Badge> : null}
        <span className="ml-auto text-xs text-muted">
          {result.evaluator_type} v{result.evaluator_version}
          {result.duration_ms !== null ? `, ${result.duration_ms} ms` : ""}
        </span>
      </div>
      {result.explanation ? (
        <p className="mt-2 text-sm text-ink-soft">{result.explanation}</p>
      ) : null}
      <div className="mt-3">
        <JsonInspector label="Evidence" value={result.evidence} defaultDepth={1} />
      </div>
    </li>
  );
}

function HumanScoreForm({ runId, onDone }: { runId: string; onDone: () => void }) {
  const toast = useToast();
  const score = useHumanScore(runId);
  const ids = { score: useId(), notes: useId(), name: useId() };
  const [value, setValue] = useState(4);
  const [verdict, setVerdict] = useState<"pass" | "fail" | "warning">("pass");
  const [notes, setNotes] = useState("");
  const [name, setName] = useState("Human review");
  const submit = (event: { preventDefault: () => void }) => {
    event.preventDefault();
    score.mutate(
      { name, score: value, max_score: 5, verdict, notes: notes || undefined },
      {
        onSuccess: () => {
          toast.show("Score recorded", "success");
          onDone();
        },
        onError: (e) => toast.show(describeError(e), "error"),
      },
    );
  };
  return (
    <form onSubmit={submit} className="grid gap-3 sm:grid-cols-2" aria-label="Record a human score">
      <label htmlFor={ids.name} className="flex flex-col gap-1 text-sm">
        <span className="text-muted">Name</span>
        <input
          id={ids.name}
          value={name}
          onChange={(e) => setName(e.target.value)}
          className="h-9 rounded-[var(--radius-control)] border border-line-strong bg-surface px-2.5"
        />
      </label>
      <label htmlFor={ids.score} className="flex flex-col gap-1 text-sm">
        <span className="text-muted">Score (0 to 5): {value}</span>
        <input
          id={ids.score}
          type="range"
          min={0}
          max={5}
          step={0.5}
          value={value}
          onChange={(e) => setValue(Number(e.target.value))}
        />
      </label>
      <fieldset className="flex flex-wrap items-center gap-3 text-sm sm:col-span-2">
        <legend className="mb-1 text-muted">Verdict</legend>
        {(["pass", "warning", "fail"] as const).map((v) => (
          <label key={v} className="flex items-center gap-1.5">
            <input
              type="radio"
              name="verdict"
              value={v}
              checked={verdict === v}
              onChange={() => setVerdict(v)}
            />
            {humanize(v)}
          </label>
        ))}
      </fieldset>
      <label htmlFor={ids.notes} className="flex flex-col gap-1 text-sm sm:col-span-2">
        <span className="text-muted">Notes</span>
        <textarea
          id={ids.notes}
          rows={3}
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          className="rounded-[var(--radius-control)] border border-line-strong bg-surface px-2.5 py-2"
        />
      </label>
      <div className="flex gap-2 sm:col-span-2">
        <Button type="submit" variant="primary" loading={score.isPending}>
          Save score
        </Button>
        <Button variant="ghost" onClick={onDone}>
          Cancel
        </Button>
      </div>
    </form>
  );
}

/**
 * Evaluations on run detail, with actions: rerun deterministic evaluations against the
 * recorded data, run a saved suite now, and record a person's score and notes.
 */
export function EvaluationsPanel({ detail }: { detail: RunDetail }) {
  const toast = useToast();
  const runId = detail.run.id;
  const rerun = useRerunEvaluations(runId);
  const evaluate = useEvaluateRun(runId);
  const suites = useEvaluationSuites();
  const [suiteId, setSuiteId] = useState("");
  const [scoring, setScoring] = useState(false);
  const finished = TERMINAL_STATUSES.has(detail.run.status);
  const nodeByStep = new Map(detail.steps.map((s) => [s.id, s.node_key]));
  const results = [...detail.evaluations].reverse();

  return (
    <div className="flex flex-col gap-4">
      <Card
        title="Evaluate this run"
        description={
          finished
            ? "Deterministic evaluations can be rerun against what the run recorded. Checks that depend on a person or the live workspace are not rerun."
            : "Actions become available when the run finishes."
        }
      >
        <div className="flex flex-wrap items-end gap-2">
          <Button
            icon={<RotateCcw aria-hidden className="size-4" />}
            disabled={!finished || detail.evaluations.length === 0}
            loading={rerun.isPending}
            onClick={() =>
              rerun.mutate(undefined, {
                onSuccess: (r) =>
                  toast.show(
                    `Reran ${r.results.length} evaluation(s)` +
                      (r.skipped.length ? `; ${r.skipped.length} not deterministic` : ""),
                    "success",
                  ),
                onError: (e) => toast.show(describeError(e), "error"),
              })
            }
          >
            Rerun deterministic evaluations
          </Button>
          <label className="flex flex-col gap-1 text-sm">
            <span className="text-muted">Suite</span>
            <select
              value={suiteId}
              onChange={(e) => setSuiteId(e.target.value)}
              disabled={!finished}
              className="h-9 rounded-[var(--radius-control)] border border-line-strong bg-surface px-2"
            >
              <option value="">Choose a suite</option>
              {(suites.data?.items ?? []).map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ))}
            </select>
          </label>
          <Button
            disabled={!finished || !suiteId}
            loading={evaluate.isPending}
            onClick={() =>
              evaluate.mutate(
                { suite_id: suiteId },
                {
                  onSuccess: (r) => toast.show(`Ran ${r.results.length} evaluation(s)`, "success"),
                  onError: (e) => toast.show(describeError(e), "error"),
                },
              )
            }
          >
            Run suite
          </Button>
          <Button variant="ghost" disabled={!finished} onClick={() => setScoring((v) => !v)}>
            Record a human score
          </Button>
        </div>
        {scoring ? (
          <div className="mt-4 border-t border-line pt-4">
            <HumanScoreForm runId={runId} onDone={() => setScoring(false)} />
          </div>
        ) : null}
      </Card>
      {results.length === 0 ? (
        <EmptyState
          title="No evaluations ran"
          description="Add evaluators to the agent profile, add an evaluation gate to a workflow, or run a suite above."
        />
      ) : (
        <ul className="flex flex-col gap-3" aria-label="Evaluation results, newest first">
          {results.map((e) => (
            <ResultItem
              key={e.id}
              result={e}
              node={e.step_run_id ? nodeByStep.get(e.step_run_id) : undefined}
            />
          ))}
        </ul>
      )}
    </div>
  );
}
