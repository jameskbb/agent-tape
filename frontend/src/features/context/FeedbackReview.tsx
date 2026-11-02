/**
 * The reviewed learning loop for one run: gaps, corrections, conflicts, and lesson
 * candidates stay pending on their run until a person classifies them, reviews
 * AgentTape's proposed destination, and approves or rejects. Nothing is promoted
 * automatically.
 */
import { useState } from "react";

import { Badge, Button, Select, controlClass, useToast } from "@/components/ui";
import { describeError } from "@/api/client";
import { humanize } from "@/lib/format";

import { useFeedbackAction, type Classification, type Destination, type Feedback } from "./api";
import { CLASSIFICATIONS, DESTINATIONS, destinationLabel } from "./labels";

const FEEDBACK_KINDS = [
  { value: "correction", label: "a correction" },
  { value: "lesson", label: "a lesson candidate" },
  { value: "gap", label: "a missing-context gap" },
  { value: "unverified", label: "an unverified inference" },
  { value: "doc_suggestion", label: "a documentation suggestion" },
] as const;

const STATUS_TONE = { pending: "warning", approved: "success", rejected: "neutral" } as const;

function PendingControls({ item, runId }: { item: Feedback; runId: string }) {
  const toast = useToast();
  const act = useFeedbackAction(runId);
  const [lesson, setLesson] = useState(item.lesson_text ?? item.text);
  const run = (a: Parameters<typeof act.mutate>[0], done: string) =>
    act.mutate(a, {
      onSuccess: () => toast.show(done, "success"),
      onError: (e) => toast.show(describeError(e), "error"),
    });
  return (
    <div className="mt-2 flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <Select
          label="Classification"
          aria-label={`Classify feedback: ${item.text}`}
          any="Choose…"
          value={item.classification ?? ""}
          disabled={act.isPending}
          options={CLASSIFICATIONS}
          onChange={(v) =>
            v &&
            run(
              { action: "classify", id: item.id, classification: v as Classification },
              "Classified; review the proposed destination",
            )
          }
        />
        {item.suggested_classification && !item.classification ? (
          <span className="text-xs text-muted">
            Reporter suggested: {humanize(item.suggested_classification)} (not applied)
          </span>
        ) : null}
      </div>
      {item.classification ? (
        <>
          <Select
            label="Proposed destination"
            aria-label="Proposed destination"
            value={item.proposed_destination ?? ""}
            disabled={act.isPending}
            options={DESTINATIONS}
            onChange={(v) =>
              run(
                { action: "propose", id: item.id, destination: v as Destination },
                "Destination updated",
              )
            }
          />
          {item.injects ? (
            <label className="flex flex-col gap-1 text-sm">
              <span className="text-muted">Lesson text future runs will receive</span>
              <textarea
                className={`${controlClass} min-h-16`}
                aria-label="Lesson text"
                value={lesson}
                onChange={(e) => setLesson(e.target.value)}
                onBlur={() =>
                  lesson.trim() &&
                  lesson !== item.lesson_text &&
                  run(
                    {
                      action: "propose",
                      id: item.id,
                      destination: item.proposed_destination ?? "project_instruction",
                      lesson_text: lesson,
                    },
                    "Lesson text saved",
                  )
                }
              />
            </label>
          ) : null}
          <div className="flex flex-wrap gap-2">
            <Button
              size="sm"
              variant="primary"
              loading={act.isPending}
              onClick={() =>
                run(
                  { action: "approve", id: item.id },
                  item.injects
                    ? "Approved: later runs in this project whose agent is given the prompt will receive this lesson"
                    : "Approved",
                )
              }
            >
              Approve {item.injects ? "lesson" : "decision"}
            </Button>
            <Button
              size="sm"
              variant="ghost"
              disabled={act.isPending}
              onClick={() => run({ action: "reject", id: item.id }, "Rejected")}
            >
              Reject
            </Button>
          </div>
        </>
      ) : (
        <div>
          <Button
            size="sm"
            variant="ghost"
            disabled={act.isPending}
            onClick={() => run({ action: "reject", id: item.id }, "Rejected")}
          >
            Reject
          </Button>
        </div>
      )}
    </div>
  );
}

function AddCorrection({ runId }: { runId: string }) {
  const toast = useToast();
  const act = useFeedbackAction(runId);
  const [text, setText] = useState("");
  const [kind, setKind] = useState("correction");
  return (
    <form
      className="flex flex-col gap-2 border-t border-line pt-3"
      onSubmit={(e) => {
        e.preventDefault();
        if (!text.trim()) return;
        act.mutate(
          { action: "create", runId, kind, text },
          {
            onSuccess: () => {
              setText("");
              toast.show("Recorded for review on this run", "success");
            },
            onError: (err) => toast.show(describeError(err), "error"),
          },
        );
      }}
    >
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <Select
          label="Add"
          aria-label="Feedback kind"
          value={kind}
          onChange={setKind}
          options={FEEDBACK_KINDS}
        />
      </div>
      <textarea
        className={`${controlClass} min-h-16`}
        aria-label="Feedback text"
        placeholder="What was wrong or missing? It stays on this run until someone approves a promotion."
        value={text}
        onChange={(e) => setText(e.target.value)}
      />
      <div>
        <Button size="sm" type="submit" loading={act.isPending} disabled={!text.trim()}>
          Record for review
        </Button>
      </div>
    </form>
  );
}

export function FeedbackReview({ runId, items }: { runId: string; items: Feedback[] }) {
  return (
    <div className="flex flex-col gap-3">
      {items.length === 0 ? (
        <p className="text-sm text-muted">
          No gaps, corrections, or lesson candidates on this run.
        </p>
      ) : (
        <ul className="flex flex-col gap-3">
          {items.map((f) => (
            <li
              key={f.id}
              className="rounded border border-line p-3 text-sm"
              data-testid="feedback-item"
            >
              <div className="flex flex-wrap items-center gap-2">
                <Badge tone={STATUS_TONE[f.status]}>{humanize(f.status)}</Badge>
                <Badge>{humanize(f.kind)}</Badge>
                <span className="text-xs text-muted">reported by {humanize(f.reported_by)}</span>
              </div>
              <p className="mt-1.5">{f.text}</p>
              {f.status === "pending" ? (
                <PendingControls item={f} runId={runId} />
              ) : (
                <p className="mt-1 text-xs text-muted">
                  {humanize(f.status)} by {f.reviewed_by ?? "a reviewer"}
                  {f.classification ? ` as ${humanize(f.classification)}` : ""}
                  {f.proposed_destination ? `; ${destinationLabel(f.proposed_destination)}` : ""}
                  {f.lesson_version != null ? `; lesson v${f.lesson_version}` : ""}
                </p>
              )}
            </li>
          ))}
        </ul>
      )}
      <AddCorrection runId={runId} />
    </div>
  );
}
