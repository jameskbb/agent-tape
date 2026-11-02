import { CircleCheck, CircleX, GitBranch, Loader } from "lucide-react";

import { cn } from "@/lib/cn";

import type { GitState, PathCheck } from "./api";

export type PathVerdict = "allowed" | "outside" | "missing" | "not-a-directory" | "invalid";

/** Classify a path check into one verdict the UI can explain. */
export function pathVerdict(check: PathCheck): PathVerdict {
  if (check.allowed) return "allowed";
  if (check.canonical === null) return "invalid";
  if (check.matched_root === null) return "outside";
  if (!check.exists) return "missing";
  return "not-a-directory";
}

const TITLES: Record<PathVerdict, string> = {
  allowed: "Allowed",
  outside: "Outside the allowlisted roots",
  missing: "Path does not exist",
  "not-a-directory": "Not a directory",
  invalid: "Not a usable path",
};

export function shortHead(head: string | null): string | null {
  return head ? head.slice(0, 10) : null;
}

export function GitStateView({ git, compact = false }: { git: GitState; compact?: boolean }) {
  if (!git.is_repo) {
    return (
      <p className="text-sm text-muted">
        Not a Git repository{git.error ? ` (${git.error})` : ""}. Diffs and Git isolation are
        unavailable here.
      </p>
    );
  }
  return (
    <div className="flex flex-col gap-2">
      <dl className="grid grid-cols-[7rem_minmax(0,1fr)] gap-x-3 gap-y-1 text-sm">
        <dt className="text-muted">Branch</dt>
        <dd className="inline-flex items-center gap-1.5">
          <GitBranch aria-hidden className="size-3.5 text-muted" />
          {git.branch ?? <span className="italic text-muted">detached HEAD</span>}
        </dd>
        <dt className="text-muted">HEAD</dt>
        <dd>
          {git.head ? (
            <code title={git.head}>{shortHead(git.head)}</code>
          ) : (
            <span className="italic text-muted">no commits yet</span>
          )}
        </dd>
        <dt className="text-muted">Working tree</dt>
        <dd className={cn(git.dirty && "text-[var(--st-cancelling)]")}>
          {git.dirty
            ? `${git.dirty_count} uncommitted change${git.dirty_count === 1 ? "" : "s"}`
            : "Clean"}
        </dd>
        <dt className="text-muted">Worktrees</dt>
        <dd>{git.worktree_supported ? "Available" : "Not available"}</dd>
      </dl>
      {!compact && git.dirty_files.length ? (
        <details className="text-sm">
          <summary className="cursor-pointer text-accent">Show uncommitted files</summary>
          <ul className="mt-1 flex flex-col gap-0.5 font-mono text-[0.8125rem] text-ink-soft">
            {git.dirty_files.map((f) => (
              <li key={f}>{f}</li>
            ))}
            {git.dirty_count > git.dirty_files.length ? (
              <li className="text-muted">and {git.dirty_count - git.dirty_files.length} more</li>
            ) : null}
          </ul>
        </details>
      ) : null}
    </div>
  );
}

/** Live explanation of a typed path: canonical form, allowlist result, and Git state. */
export function PathCheckView({
  check,
  loading,
}: {
  check: PathCheck | undefined;
  loading: boolean;
}) {
  if (!check) {
    return loading ? (
      <p className="flex items-center gap-1.5 text-sm text-muted">
        <Loader aria-hidden className="size-3.5 animate-spin" /> Checking the path...
      </p>
    ) : null;
  }
  const verdict = pathVerdict(check);
  const ok = verdict === "allowed";
  return (
    <div
      aria-live="polite"
      data-verdict={verdict}
      className={cn(
        "flex flex-col gap-2 rounded-[var(--radius-control)] border px-3 py-2.5 text-sm",
        ok
          ? "border-[color-mix(in_srgb,var(--st-succeeded)_40%,transparent)] bg-[var(--st-succeeded-bg)]"
          : "border-[color-mix(in_srgb,var(--st-failed)_40%,transparent)] bg-[var(--st-failed-bg)]",
      )}
    >
      <p className="flex items-center gap-1.5 font-medium text-ink">
        {ok ? (
          <CircleCheck aria-hidden className="size-4 text-[var(--st-succeeded)]" />
        ) : (
          <CircleX aria-hidden className="size-4 text-[var(--st-failed)]" />
        )}
        {TITLES[verdict]}
        {loading ? <Loader aria-hidden className="size-3.5 animate-spin text-muted" /> : null}
      </p>
      <p className="text-ink-soft">{check.message}</p>
      {check.canonical ? (
        <p className="text-xs text-muted">
          Canonical path: <code className="text-ink-soft">{check.canonical}</code>
        </p>
      ) : null}
      {verdict === "outside" ? (
        <div className="text-xs text-muted">
          Allowed roots:
          <ul className="mt-0.5">
            {check.allowed_roots.map((r) => (
              <li key={r}>
                <code>{r}</code>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {ok && check.git ? <GitStateView git={check.git} compact /> : null}
    </div>
  );
}
