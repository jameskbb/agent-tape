import { ChevronDown, ChevronRight, FileWarning, ShieldAlert } from "lucide-react";
import { useMemo, useState } from "react";

import { Badge, Button } from "@/components/ui";
import { cn } from "@/lib/cn";
import { formatBytes, humanize } from "@/lib/format";

import type { ChangedFile, DiffLine } from "./api";
import { languageFor, tokenize, type TokenKind } from "./highlight";

const TOKEN_CLASS: Record<TokenKind, string> = {
  plain: "",
  keyword: "text-[var(--st-waiting)]",
  string: "text-[var(--st-timed-out)]",
  comment: "text-faint italic",
  number: "text-[var(--st-preparing)]",
  key: "text-accent",
};

const CHANGE_TONE = {
  added: "success",
  deleted: "danger",
  renamed: "violet",
  modified: "neutral",
} as const;

const EDGE: Record<ChangedFile["change_type"], string> = {
  added: "var(--st-succeeded)",
  deleted: "var(--st-failed)",
  renamed: "var(--st-waiting)",
  modified: "var(--line-strong)",
};

/** Share of the run's changed lines this file accounts for, split into added and removed. */
function ChangeBar({ file, total }: { file: ChangedFile; total: number }) {
  if (file.is_binary || total === 0) return null;
  const ins = file.insertions ?? 0;
  const del = file.deletions ?? 0;
  const width = Math.max(4, Math.round(((ins + del) / total) * 64));
  return (
    <span
      aria-hidden
      className="hidden h-1.5 overflow-hidden rounded-full bg-sunken sm:flex"
      style={{ width: 64 }}
    >
      <span className="flex h-full" style={{ width }}>
        <span className="h-full bg-[var(--st-succeeded)]" style={{ flex: ins || 0.0001 }} />
        <span className="h-full bg-[var(--st-failed)]" style={{ flex: del || 0.0001 }} />
      </span>
    </span>
  );
}

function Line({ line, lang }: { line: DiffLine; lang: string | null }) {
  if (line.kind === "note") {
    return (
      <tr>
        <td colSpan={3} className="px-3 py-0.5 text-xs text-muted italic">
          {line.text}
        </td>
      </tr>
    );
  }
  const sign = line.kind === "add" ? "+" : line.kind === "del" ? "-" : " ";
  return (
    <tr
      data-line={line.kind}
      className={cn(
        line.kind === "add" && "bg-[color-mix(in_srgb,var(--st-succeeded)_11%,transparent)]",
        line.kind === "del" && "bg-[color-mix(in_srgb,var(--st-failed)_11%,transparent)]",
      )}
    >
      <td className="w-12 border-r border-line px-2 text-right text-faint select-none">
        {line.old ?? ""}
      </td>
      <td className="w-12 border-r border-line px-2 text-right text-faint select-none">
        {line.new ?? ""}
      </td>
      <td className="px-3 whitespace-pre">
        <span
          aria-hidden
          className={cn(
            "mr-2 inline-block w-2 select-none",
            line.kind === "add" && "text-[var(--st-succeeded)]",
            line.kind === "del" && "text-[var(--st-failed)]",
          )}
        >
          {sign}
        </span>
        <span className="sr-only">
          {line.kind === "add" ? "added: " : line.kind === "del" ? "removed: " : ""}
        </span>
        {tokenize(line.text, lang).map((t, i) => (
          <span key={i} className={TOKEN_CLASS[t.kind]}>
            {t.text}
          </span>
        ))}
      </td>
    </tr>
  );
}

function missingDiffReason(file: ChangedFile): string {
  if (file.change_type === "renamed" && file.insertions === 0 && file.deletions === 0) {
    return "Renamed without content changes.";
  }
  if (file.diff_omitted === "count_cap") {
    return "The diff for this file was not captured: the run created more untracked files than AgentTape diffs, so this one was left out. Check it in the workspace before approving a commit.";
  }
  if (file.diff_omitted === "size_cap") {
    return "The diff for this file was not captured: the file is over the size cap. Check it in the workspace before approving a commit.";
  }
  return "The diff for this file was not captured (it was over the size cap or recorded by the adapter without a diff).";
}

function FileBody({ file }: { file: ChangedFile }) {
  const lang = useMemo(() => languageFor(file.path), [file.path]);
  if (file.is_binary) {
    return (
      <p className="px-4 py-3 text-sm text-muted">
        Binary file: content is not stored or shown.
        {file.size_bytes !== null ? ` Size ${formatBytes(file.size_bytes)}.` : ""}
      </p>
    );
  }
  if (!file.diff_available) {
    return <p className="px-4 py-3 text-sm text-muted">{missingDiffReason(file)}</p>;
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse font-mono text-[0.8125rem] leading-5">
        <caption className="sr-only">Diff of {file.path}</caption>
        {file.hunks.map((hunk, h) => (
          <tbody key={h}>
            <tr className="bg-sunken">
              <td colSpan={3} className="px-3 py-1 text-xs text-muted">
                {hunk.header}
              </td>
            </tr>
            {hunk.lines.map((line, i) => (
              <Line key={i} line={line} lang={lang} />
            ))}
          </tbody>
        ))}
      </table>
      {file.diff_truncated ? (
        <p className="border-t border-line px-4 py-2 text-xs text-muted">
          This file's diff was truncated at the size cap. Download changes.diff from the Artifacts
          tab for what was stored.
        </p>
      ) : null}
    </div>
  );
}

function FileStrip({
  file,
  total,
  open,
  onToggle,
}: {
  file: ChangedFile;
  total: number;
  open: boolean;
  onToggle: () => void;
}) {
  const bodyId = `diff-${file.id}`;
  return (
    <li
      className="overflow-hidden rounded-[var(--radius-panel)] border border-line bg-surface"
      style={{ borderLeft: `3px solid ${EDGE[file.change_type]}` }}
      data-file={file.path}
    >
      <button
        type="button"
        aria-expanded={open}
        aria-controls={bodyId}
        onClick={onToggle}
        className="flex w-full flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2 text-left hover:bg-sunken"
      >
        {open ? (
          <ChevronDown aria-hidden className="size-4 shrink-0 text-muted" />
        ) : (
          <ChevronRight aria-hidden className="size-4 shrink-0 text-muted" />
        )}
        <Badge tone={CHANGE_TONE[file.change_type]}>{humanize(file.change_type)}</Badge>
        <span className="min-w-0 flex-1 truncate">
          <code className="text-ink">{file.path}</code>
          {file.old_path ? (
            <span className="ml-2 text-xs text-muted">
              from <code>{file.old_path}</code>
            </span>
          ) : null}
        </span>
        {file.is_protected ? (
          <Badge tone="warning" title={`Matches ${file.protected_patterns.join(", ")}`}>
            <ShieldAlert aria-hidden className="size-3" /> Protected path
          </Badge>
        ) : null}
        {file.pre_existing_dirty ? (
          <Badge title="This file already had uncommitted changes when the run started">
            <FileWarning aria-hidden className="size-3" /> Changed before the run
          </Badge>
        ) : null}
        <ChangeBar file={file} total={total} />
        <span className="w-24 text-right text-sm tabular-nums">
          {file.is_binary ? (
            <span className="text-muted">binary</span>
          ) : (
            <>
              <span className="text-[var(--st-succeeded)]">+{file.insertions ?? "?"}</span>{" "}
              <span className="text-[var(--st-failed)]">-{file.deletions ?? "?"}</span>
            </>
          )}
        </span>
      </button>
      {open ? (
        <div id={bodyId} className="border-t border-line">
          <FileBody file={file} />
        </div>
      ) : null}
    </li>
  );
}

/** Collapsible, syntax-coloured diff of every changed file. */
export function DiffViewer({ files }: { files: ChangedFile[] }) {
  const total = files.reduce((n, f) => n + (f.insertions ?? 0) + (f.deletions ?? 0), 0);
  const expandByDefault = files.length <= 8 && total <= 600;
  const [open, setOpen] = useState<Record<string, boolean>>(() =>
    Object.fromEntries(files.map((f) => [f.id, expandByDefault && !f.is_binary])),
  );
  const allOpen = files.every((f) => open[f.id]);
  return (
    <section aria-label="Diff" className="flex flex-col gap-2">
      <div className="flex justify-end">
        <Button
          size="sm"
          variant="ghost"
          onClick={() => setOpen(Object.fromEntries(files.map((f) => [f.id, !allOpen])))}
        >
          {allOpen ? "Collapse all files" : "Expand all files"}
        </Button>
      </div>
      <ul className="flex flex-col gap-2">
        {files.map((f) => (
          <FileStrip
            key={f.id}
            file={f}
            total={total}
            open={Boolean(open[f.id])}
            onToggle={() => setOpen((o) => ({ ...o, [f.id]: !o[f.id] }))}
          />
        ))}
      </ul>
    </section>
  );
}
