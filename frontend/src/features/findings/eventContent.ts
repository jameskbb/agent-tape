/**
 * Pure helpers for an event citation's body: the recorded event's own content,
 * numbered the same way the backend numbers it, and picked by the same content-key order
 * the backend's evidence names. No network request here: event citations render from the
 * in-memory `events` feed the run detail page already holds.
 */
import type { ChangedFile, DiffHunk, DiffLine } from "@/features/changes/api";

/** A `cat -n`-shaped line: right-aligned line number, then a TAB or U+2192 separator, then
 * the content. Mirrors `agenttape.findings.normalize.strip_cat_n` exactly (both separators
 * some Claude Code versions emit). */
const CAT_N_LINE = /^\s*(\d+)(?:\t|→)(.*)$/;

/** `(absolute line number, content)` pairs: the `cat -n` numbers when the text is shaped
 * that way, else every line numbered from 1 (the same fallback
 * `agenttape.findings.verify.decide_event_citation` applies). */
export function numberedLines(text: string): [number, string][] {
  const lines = text.split("\n");
  const pairs: [number, string][] = [];
  for (const line of lines) {
    const match = CAT_N_LINE.exec(line);
    if (match) pairs.push([Number(match[1]), match[2] ?? ""]);
  }
  if (pairs.length > 0) return pairs;
  return lines.map((line, i): [number, string] => [i + 1, line]);
}

/** The order `agenttape.findings.evidence._EVENT_CONTENT_KEYS` prefers when a verdict's
 * evidence names no `content_key` of its own. */
const EVENT_CONTENT_KEYS = ["output", "diff", "text", "content", "body", "message"] as const;

/** `value` as a plain object, or null for anything else (null, an array, a primitive). */
export function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

/** The recorded event's own content and the key it came from: `evidence.event.content_key`
 * when the verdict named one and the event still carries it, else the first of
 * `EVENT_CONTENT_KEYS` present on the event's payload. Neither the agent's quote nor any
 * other field is ever substituted. */
export function eventContentOf(
  event: { payload: Record<string, unknown> },
  evidence: Record<string, unknown>,
): { key: string | null; content: string | null } {
  const eventInfo = record(evidence["event"]);
  const preferredKey =
    eventInfo && typeof eventInfo["content_key"] === "string" ? eventInfo["content_key"] : null;
  if (preferredKey) {
    const value = event.payload[preferredKey];
    if (typeof value === "string") return { key: preferredKey, content: value };
  }
  for (const key of EVENT_CONTENT_KEYS) {
    const value = event.payload[key];
    if (typeof value === "string") return { key, content: value };
  }
  return { key: null, content: null };
}

const HUNK_RE = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$/;

/** A single-file `ChangedFile` from unified diff text, for `DiffViewer`; `null` when
 * `text` has no `@@ ... @@` hunk header at all (not diff-shaped). Every field `DiffViewer`
 * does not need to render a plain, single-file diff is set to its honest default (`null`,
 * `false`, or `[]`) rather than guessed. */
export function parseUnifiedDiff(path: string, text: string): ChangedFile | null {
  const hunks: DiffHunk[] = [];
  let current: DiffHunk | null = null;
  let oldNo = 0;
  let newNo = 0;
  let insertions = 0;
  let deletions = 0;

  for (const raw of text.split("\n")) {
    const hunkMatch = HUNK_RE.exec(raw);
    if (hunkMatch) {
      current = { header: raw, section: hunkMatch[5] ?? "", lines: [] };
      hunks.push(current);
      oldNo = Number(hunkMatch[1]);
      newNo = Number(hunkMatch[3]);
      continue;
    }
    if (!current) continue; // text before the first hunk (---/+++ headers, prose) is skipped
    const line = addLine(current, raw, oldNo, newNo);
    if (line === null) continue;
    oldNo = line.old;
    newNo = line.new;
    if (line.kind === "add") insertions += 1;
    if (line.kind === "del") deletions += 1;
  }

  if (hunks.length === 0) return null;
  return {
    id: path,
    path,
    old_path: null,
    change_type: "modified",
    insertions,
    deletions,
    is_binary: false,
    size_bytes: null,
    is_protected: false,
    protected_patterns: [],
    pre_existing_dirty: false,
    attempt_id: null,
    hunks,
    diff_available: true,
    diff_truncated: false,
  };
}

/** Appends one diff line to `hunk` and returns the next `(old, new)` counters, or `null`
 * for a line this parser does not append (an escape note is appended but advances neither
 * counter). */
function addLine(
  hunk: DiffHunk,
  raw: string,
  oldNo: number,
  newNo: number,
): { kind: DiffLine["kind"]; old: number; new: number } | null {
  if (raw.startsWith("+") && !raw.startsWith("+++")) {
    hunk.lines.push({ kind: "add", text: raw.slice(1), old: null, new: newNo });
    return { kind: "add", old: oldNo, new: newNo + 1 };
  }
  if (raw.startsWith("-") && !raw.startsWith("---")) {
    hunk.lines.push({ kind: "del", text: raw.slice(1), old: oldNo, new: null });
    return { kind: "del", old: oldNo + 1, new: newNo };
  }
  if (raw.startsWith("\\")) {
    hunk.lines.push({ kind: "note", text: raw.slice(1).trim(), old: null, new: null });
    return { kind: "note", old: oldNo, new: newNo };
  }
  if (raw === "" || raw.startsWith(" ")) {
    hunk.lines.push({ kind: "ctx", text: raw.slice(1), old: oldNo, new: newNo });
    return { kind: "ctx", old: oldNo + 1, new: newNo + 1 };
  }
  return null; // an unrecognized header-ish line between hunks
}
