import { Check, Play, Sparkles } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { useNavigate } from "react-router";

import { describeError } from "@/api/client";
import { Page } from "@/components/layout/Page";
import { Badge, Button, EmptyState, ErrorState, LoadingBlock, useToast } from "@/components/ui";
import { cn } from "@/lib/cn";
import { useProfiles, type AgentProfile } from "@/features/agents/api";
import { useProjects, type Project } from "@/features/projects/api";
import { ProjectCreateForm } from "@/features/projects/ProjectCreateForm";
import { usePrompts, type PromptTemplate } from "@/features/prompts/api";
import { serverIssueText } from "@/features/prompts/PromptCreatePage";
import { RenderedPreview, useLivePreview } from "@/features/prompts/RenderedPreview";
import { VariableInputs } from "@/features/prompts/VariableInputs";
import { buildInputs, initialForm, type FormValue } from "@/features/prompts/variables";

import { useCreateRun, useOnboardingAction, useSeedDemo } from "./api";

const DEMO_PROJECT = "AgentTape Demo Project";
const DEMO_AGENT = "Demo Agent";

const STEPS = ["Project", "Agent", "Prompt", "Start"] as const;

function Choice({
  selected,
  onSelect,
  title,
  meta,
  children,
  name,
}: {
  selected: boolean;
  onSelect: () => void;
  title: ReactNode;
  meta?: ReactNode;
  children?: ReactNode;
  name: string;
}) {
  return (
    <label
      className={cn(
        "flex cursor-pointer gap-3 rounded-[var(--radius-control)] border px-3 py-2.5 transition-colors",
        selected ? "border-accent bg-accent-soft" : "border-line bg-surface hover:bg-sunken",
      )}
    >
      <input
        type="radio"
        name={name}
        checked={selected}
        onChange={onSelect}
        className="mt-1 accent-[var(--accent)]"
      />
      <span className="min-w-0 flex-1">
        <span className="flex flex-wrap items-center gap-2 font-medium">
          {title}
          {meta}
        </span>
        {children ? <span className="mt-0.5 block text-sm text-muted">{children}</span> : null}
      </span>
    </label>
  );
}

function Stepper({ step, onJump }: { step: number; onJump: (i: number) => void }) {
  return (
    <ol className="flex flex-wrap gap-2" aria-label="Setup steps">
      {STEPS.map((label, i) => {
        const done = i < step;
        const current = i === step;
        return (
          <li key={label}>
            <button
              type="button"
              disabled={i > step}
              aria-current={current ? "step" : undefined}
              onClick={() => onJump(i)}
              className={cn(
                "flex items-center gap-2 rounded-full border py-1 pr-3 pl-1 text-sm",
                current
                  ? "border-accent bg-accent-soft font-medium text-ink"
                  : done
                    ? "border-line bg-surface text-ink-soft hover:border-line-strong"
                    : "border-line bg-surface text-faint",
              )}
            >
              <span
                className={cn(
                  "flex size-6 items-center justify-center rounded-full text-xs tabular-nums",
                  current
                    ? "bg-accent text-accent-ink"
                    : done
                      ? "bg-[var(--st-succeeded-bg)] text-[var(--st-succeeded)]"
                      : "bg-sunken",
                )}
              >
                {done ? <Check aria-hidden className="size-3.5" /> : i + 1}
              </span>
              {label}
            </button>
          </li>
        );
      })}
    </ol>
  );
}

function ProjectStep({
  selected,
  onSelect,
}: {
  selected: Project | null;
  onSelect: (p: Project) => void;
}) {
  const projects = useProjects({ limit: 50 });
  const seed = useSeedDemo();
  const toast = useToast();
  const [adding, setAdding] = useState(false);
  useEffect(() => {
    const demo = projects.data?.items.find((p) => p.name === DEMO_PROJECT);
    if (!selected && demo) onSelect(demo);
  }, [projects.data, selected, onSelect]);
  if (projects.isPending) return <LoadingBlock label="Loading projects" />;
  if (projects.isError)
    return <ErrorState error={projects.error} onRetry={() => void projects.refetch()} />;
  const items = [...projects.data.items].sort((a, b) =>
    a.name === DEMO_PROJECT ? -1 : b.name === DEMO_PROJECT ? 1 : a.name.localeCompare(b.name),
  );
  const hasDemo = items.some((p) => p.name === DEMO_PROJECT);
  return (
    <div className="flex flex-col gap-4">
      <p className="text-ink-soft">
        Runs happen inside a project. Start with the demo project, a small repository AgentTape
        owns, or add one of yours from an allowlisted directory.
      </p>
      {!hasDemo ? (
        <div className="flex flex-wrap items-center gap-3 rounded-[var(--radius-control)] border border-dashed border-line-strong px-3 py-3">
          <p className="min-w-0 flex-1 text-sm text-muted">
            The demo project and Demo Agent profiles are not set up yet.
          </p>
          <Button
            loading={seed.isPending}
            onClick={() =>
              seed.mutate(undefined, {
                onSuccess: () => toast.show("Demo data created", "success"),
                onError: (e) => toast.show(describeError(e), "error"),
              })
            }
          >
            Create demo data
          </Button>
        </div>
      ) : null}
      {items.length ? (
        <fieldset className="flex flex-col gap-2">
          <legend className="sr-only">Project</legend>
          {items.map((p) => (
            <Choice
              key={p.id}
              name="project"
              selected={selected?.id === p.id}
              onSelect={() => onSelect(p)}
              title={p.name}
              meta={p.name === DEMO_PROJECT ? <Badge tone="accent">Recommended</Badge> : null}
            >
              <code className="text-xs">{p.repo_path}</code>
            </Choice>
          ))}
        </fieldset>
      ) : null}
      {adding ? (
        <div className="rounded-[var(--radius-panel)] border border-line p-4">
          <h3 className="mb-3 text-base font-semibold">Add your own project</h3>
          <ProjectCreateForm
            showSettings={false}
            submitLabel="Add and use this project"
            onCancel={() => setAdding(false)}
            onCreated={(p) => {
              setAdding(false);
              onSelect(p);
              toast.show(`Added ${p.name}`, "success");
            }}
          />
        </div>
      ) : (
        <div>
          <Button variant="ghost" onClick={() => setAdding(true)}>
            Add your own project instead
          </Button>
        </div>
      )}
    </div>
  );
}

function AgentStep({
  selected,
  onSelect,
}: {
  selected: AgentProfile | null;
  onSelect: (p: AgentProfile) => void;
}) {
  const profiles = useProfiles({ adapter_type: "demo", limit: 50 });
  useEffect(() => {
    const demo = profiles.data?.items.find((p) => p.name === DEMO_AGENT);
    if (!selected && demo) onSelect(demo);
  }, [profiles.data, selected, onSelect]);
  if (profiles.isPending) return <LoadingBlock label="Loading agents" />;
  if (profiles.isError)
    return <ErrorState error={profiles.error} onRetry={() => void profiles.refetch()} />;
  const items = [...profiles.data.items].sort((a, b) =>
    a.name === DEMO_AGENT ? -1 : b.name === DEMO_AGENT ? 1 : a.name.localeCompare(b.name),
  );
  if (!items.length) {
    return (
      <EmptyState
        title="No Demo Agent profiles"
        description="Go back one step and create the demo data, then return here."
      />
    );
  }
  return (
    <div className="flex flex-col gap-4">
      <p className="text-ink-soft">
        The Demo Agent needs no credentials. It runs real commands, makes a small change in its own
        copy of the demo repository, and reports simulated token usage clearly labelled as such.
        Claude Code, Codex, and your own commands can be set up later under Agents.
      </p>
      <fieldset className="flex flex-col gap-2">
        <legend className="sr-only">Agent</legend>
        {items.map((p) => (
          <Choice
            key={p.id}
            name="agent"
            selected={selected?.id === p.id}
            onSelect={() => onSelect(p)}
            title={p.name}
            meta={p.name === DEMO_AGENT ? <Badge tone="accent">Recommended</Badge> : null}
          >
            {p.description}
          </Choice>
        ))}
      </fieldset>
    </div>
  );
}

function PromptStep({
  selected,
  onSelect,
  form,
  onForm,
}: {
  selected: PromptTemplate | null;
  onSelect: (p: PromptTemplate) => void;
  form: Record<string, FormValue>;
  onForm: (f: Record<string, FormValue>) => void;
}) {
  const prompts = usePrompts({ limit: 50, sort: "name" });
  const defs = selected?.latest?.variables ?? [];
  const inputs = buildInputs(defs, form);
  const preview = useLivePreview(selected?.id ?? null, selected?.latest?.id ?? null, inputs);
  if (prompts.isPending) return <LoadingBlock label="Loading prompts" />;
  if (prompts.isError)
    return <ErrorState error={prompts.error} onRetry={() => void prompts.refetch()} />;
  if (!prompts.data.items.length) {
    return (
      <EmptyState
        title="No prompts yet"
        description="Create the demo data in the first step to get a few ready-made prompts."
      />
    );
  }
  return (
    <div className="grid gap-5 lg:grid-cols-[minmax(0,20rem)_minmax(0,1fr)]">
      <fieldset className="flex flex-col gap-2">
        <legend className="mb-1 text-sm font-medium text-ink-soft">Prompt</legend>
        {prompts.data.items.map((p) => (
          <Choice
            key={p.id}
            name="prompt"
            selected={selected?.id === p.id}
            onSelect={() => onSelect(p)}
            title={p.name}
            meta={<Badge>v{p.latest_version}</Badge>}
          >
            {p.description}
          </Choice>
        ))}
      </fieldset>
      {selected ? (
        <div className="flex flex-col gap-4">
          <VariableInputs
            definitions={defs}
            values={form}
            onChange={onForm}
            issues={preview.data?.issues}
          />
          <RenderedPreview preview={preview} />
        </div>
      ) : (
        <p className="text-sm text-muted">Choose a prompt to fill in its variables.</p>
      )}
    </div>
  );
}

export function OnboardingPage() {
  const [step, setStep] = useState(-1);
  const [project, setProject] = useState<Project | null>(null);
  const [agent, setAgent] = useState<AgentProfile | null>(null);
  const [prompt, setPrompt] = useState<PromptTemplate | null>(null);
  const [form, setForm] = useState<Record<string, FormValue>>({});
  const action = useOnboardingAction();
  const createRun = useCreateRun();
  const navigate = useNavigate();
  const toast = useToast();

  const skip = () =>
    action.mutate("dismiss", {
      onSuccess: () => void navigate("/"),
      onError: (e) => toast.show(describeError(e), "error"),
    });

  const start = () => {
    if (!project || !agent || !prompt) return;
    createRun.mutate(
      {
        project_id: project.id,
        agent_profile_id: agent.id,
        prompt_template_id: prompt.id,
        prompt_version_id: prompt.latest?.id ?? null,
        inputs: buildInputs(prompt.latest?.variables ?? [], form),
        source: "ui",
        tags: ["onboarding"],
      },
      {
        onSuccess: (result) => {
          // The run already started; failing to record completion only affects the banner.
          action.mutate("complete", {
            onError: (e) =>
              toast.show(`Setup progress was not saved: ${describeError(e)}`, "error"),
          });
          toast.show("Your first run is recording", "success");
          void navigate(`/runs/${result.run.id}`);
        },
        onError: (e) => toast.show(serverIssueText(e), "error"),
      },
    );
  };

  if (step < 0) {
    return (
      <Page title="Welcome to AgentTape">
        <section className="flex max-w-[64ch] flex-col gap-4">
          <p className="text-lg text-ink-soft">
            AgentTape records everything an AI coding agent does: the prompt and configuration that
            started it, every command and tool call, the files it changed, and whether the result
            passed its checks.
          </p>
          <p className="text-ink-soft">
            In four short steps you will choose a project, an agent, and a prompt, then watch your
            first run stream in live. The built-in Demo Agent needs no credentials.
          </p>
          <div className="flex flex-wrap gap-2">
            <Button
              variant="primary"
              icon={<Sparkles className="size-4" aria-hidden />}
              onClick={() => setStep(0)}
            >
              Get started
            </Button>
            <Button variant="ghost" onClick={skip} loading={action.isPending}>
              Skip for now
            </Button>
          </div>
        </section>
      </Page>
    );
  }

  const canNext =
    (step === 0 && project !== null) ||
    (step === 1 && agent !== null) ||
    (step === 2 && prompt !== null);

  return (
    <Page
      title="Set up your first run"
      actions={
        <Button variant="ghost" onClick={skip}>
          Skip for now
        </Button>
      }
    >
      <Stepper step={step} onJump={setStep} />
      <section
        aria-label={STEPS[step]}
        className="rounded-[var(--radius-panel)] border border-line bg-surface p-5"
      >
        <h2 className="mb-4 text-lg font-semibold">
          {step === 0
            ? "Choose a project"
            : step === 1
              ? "Choose an agent"
              : step === 2
                ? "Pick a prompt and fill it in"
                : "Start the run"}
        </h2>
        {step === 0 ? (
          <ProjectStep selected={project} onSelect={setProject} />
        ) : step === 1 ? (
          <AgentStep selected={agent} onSelect={setAgent} />
        ) : step === 2 ? (
          <PromptStep
            selected={prompt}
            onSelect={(p) => {
              setPrompt(p);
              setForm(initialForm(p.latest?.variables ?? []));
            }}
            form={form}
            onForm={setForm}
          />
        ) : (
          <div className="flex flex-col gap-4">
            <dl className="grid max-w-[40rem] grid-cols-[7rem_minmax(0,1fr)] gap-x-4 gap-y-2 text-sm">
              <dt className="text-muted">Project</dt>
              <dd className="font-medium">{project?.name}</dd>
              <dt className="text-muted">Agent</dt>
              <dd className="font-medium">{agent?.name}</dd>
              <dt className="text-muted">Prompt</dt>
              <dd className="font-medium">
                {prompt?.name} <span className="text-muted">v{prompt?.latest_version}</span>
              </dd>
            </dl>
            <p className="max-w-[64ch] text-sm text-muted">
              The run goes through the same engine as every other run. You will land on its timeline
              and see events arrive as they happen.
            </p>
            <div>
              <Button
                variant="primary"
                icon={<Play className="size-4" aria-hidden />}
                loading={createRun.isPending}
                onClick={start}
              >
                Start the run
              </Button>
            </div>
          </div>
        )}
      </section>
      <div className="flex justify-between gap-2">
        <Button onClick={() => setStep(step - 1)}>Back</Button>
        {step < 3 ? (
          <Button variant="primary" disabled={!canNext} onClick={() => setStep(step + 1)}>
            Continue
          </Button>
        ) : null}
      </div>
    </Page>
  );
}
