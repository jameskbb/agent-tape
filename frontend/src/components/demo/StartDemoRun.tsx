import { Play } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import { useNavigate } from "react-router";

import { describeError } from "@/api/client";
import { useDemoScenarios, useStartDemoRun } from "@/api/hooks";
import { Button, useToast, type ButtonProps } from "@/components/ui";
import { cn } from "@/lib/cn";

/** Opens a picker of Demo Agent scenarios and starts a live run through the real engine. */
export function StartDemoRun({
  label = "Start a demo run",
  variant = "primary",
  size,
}: {
  label?: string;
  variant?: ButtonProps["variant"];
  size?: ButtonProps["size"];
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const scenarios = useDemoScenarios();
  const start = useStartDemoRun();
  const navigate = useNavigate();
  const toast = useToast();
  const [choice, setChoice] = useState("success");

  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) d.showModal();
    if (!open && d.open) d.close();
  }, [open]);

  const submit = () => {
    start.mutate(choice, {
      onSuccess: (result) => {
        setOpen(false);
        toast.show("Demo run started", "success");
        void navigate(`/runs/${result.run.id}`);
      },
      onError: (error) => toast.show(describeError(error), "error"),
    });
  };

  return (
    <>
      <Button
        variant={variant}
        size={size}
        icon={<Play className="size-4" aria-hidden />}
        onClick={() => setOpen(true)}
      >
        {label}
      </Button>
      <dialog
        ref={ref}
        aria-labelledby={titleId}
        onClose={() => setOpen(false)}
        className="m-auto w-[min(94vw,34rem)] rounded-[var(--radius-panel)] border border-line bg-raised p-0 text-ink shadow-[var(--shadow-pop)] backdrop:bg-[rgb(10_16_24/0.45)]"
      >
        <form
          className="flex flex-col gap-4 p-5"
          onSubmit={(e) => {
            e.preventDefault();
            submit();
          }}
        >
          <div>
            <h2 id={titleId} className="text-lg font-semibold">
              Start a Demo Agent run
            </h2>
            <p className="mt-1 text-sm text-muted">
              The Demo Agent needs no credentials. It runs real commands in a copy of a small
              fixture repository that AgentTape owns, so you can watch a genuine timeline, diff, and
              evaluation.
            </p>
          </div>
          <fieldset className="flex flex-col gap-2">
            <legend className="sr-only">Scenario</legend>
            {(scenarios.data ?? []).map((s) => (
              <label
                key={s.scenario}
                className={cn(
                  "flex cursor-pointer gap-3 rounded-[var(--radius-control)] border px-3 py-2.5 transition-colors",
                  choice === s.scenario
                    ? "border-accent bg-accent-soft"
                    : "border-line hover:bg-sunken",
                )}
              >
                <input
                  type="radio"
                  name="scenario"
                  value={s.scenario}
                  checked={choice === s.scenario}
                  onChange={() => setChoice(s.scenario)}
                  className="mt-1 accent-[var(--accent)]"
                />
                <span>
                  <span className="block font-medium">{s.profile_name}</span>
                  <span className="block text-sm text-muted">{s.description}</span>
                </span>
              </label>
            ))}
            {scenarios.isError ? (
              <p className="text-sm text-[var(--st-failed)]">{describeError(scenarios.error)}</p>
            ) : null}
          </fieldset>
          <div className="flex justify-end gap-2">
            <Button onClick={() => setOpen(false)}>Close</Button>
            <Button
              type="submit"
              variant="primary"
              loading={start.isPending}
              disabled={!scenarios.data}
            >
              Start run
            </Button>
          </div>
        </form>
      </dialog>
    </>
  );
}
