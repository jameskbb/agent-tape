import { ChevronDown, ChevronRight, Copy } from "lucide-react";
import { useState } from "react";

import { cn } from "@/lib/cn";

import { IconButton } from "./Button";

export function CodeBlock({
  code,
  label,
  className,
  maxHeight = "24rem",
}: {
  code: string;
  label?: string;
  className?: string;
  maxHeight?: string;
}) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    navigator.clipboard
      .writeText(code)
      .then(() => {
        setCopied(true);
        setTimeout(() => setCopied(false), 1500);
      })
      .catch(() => undefined);
  };
  return (
    <div
      className={cn(
        "relative rounded-[var(--radius-control)] border border-line bg-sunken",
        className,
      )}
    >
      {label ? (
        <div className="border-b border-line px-3 py-1.5 text-xs text-muted">{label}</div>
      ) : null}
      <IconButton
        size="sm"
        label={copied ? "Copied" : "Copy to clipboard"}
        icon={<Copy className="size-3.5" />}
        onClick={copy}
        className="absolute top-1 right-1"
      />
      {/* Focusable so keyboard users can scroll long output (WCAG scrollable regions). */}
      <pre
        tabIndex={0}
        aria-label={label ?? "Code"}
        className="overflow-auto p-3 pr-10 font-mono text-[0.8125rem] leading-relaxed whitespace-pre-wrap break-all text-ink-soft"
        style={{ maxHeight }}
      >
        {code}
      </pre>
    </div>
  );
}

type Json = string | number | boolean | null | Json[] | { [key: string]: Json };

/** Collapsible JSON tree (raw inspector). Deep nodes start collapsed. */
export function JsonInspector({
  value,
  label,
  defaultDepth = 1,
}: {
  value: unknown;
  label?: string;
  defaultDepth?: number;
}) {
  return (
    <div className="rounded-[var(--radius-control)] border border-line bg-sunken">
      {label ? (
        <div className="border-b border-line px-3 py-1.5 text-xs text-muted">{label}</div>
      ) : null}
      <div className="overflow-auto p-3 font-mono text-[0.8125rem] leading-relaxed">
        <JsonNode value={value as Json} depth={0} defaultDepth={defaultDepth} />
      </div>
    </div>
  );
}

function JsonNode({
  value,
  depth,
  defaultDepth,
  name,
}: {
  value: Json;
  depth: number;
  defaultDepth: number;
  name?: string;
}) {
  const [open, setOpen] = useState(depth < defaultDepth);
  const keyLabel =
    name !== undefined ? <span className="text-accent">{JSON.stringify(name)}: </span> : null;
  if (value === null || typeof value !== "object") {
    const color =
      typeof value === "string"
        ? "text-[var(--st-succeeded)]"
        : typeof value === "number"
          ? "text-[var(--st-waiting)]"
          : "text-[var(--st-cancelling)]";
    return (
      <div className="break-all whitespace-pre-wrap">
        {keyLabel}
        <span className={color}>{JSON.stringify(value)}</span>
      </div>
    );
  }
  const entries: [string, Json][] = Array.isArray(value)
    ? value.map((v, i) => [String(i), v])
    : Object.entries(value);
  const [openBrace, closeBrace] = Array.isArray(value) ? ["[", "]"] : ["{", "}"];
  if (entries.length === 0) {
    return (
      <div>
        {keyLabel}
        <span className="text-muted">{openBrace + closeBrace}</span>
      </div>
    );
  }
  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        className="inline-flex items-center text-left hover:text-ink"
      >
        {open ? (
          <ChevronDown aria-hidden className="size-3" />
        ) : (
          <ChevronRight aria-hidden className="size-3" />
        )}
        {keyLabel}
        <span className="text-muted">
          {openBrace}
          {open
            ? ""
            : ` ${entries.length} ${Array.isArray(value) ? "items" : "keys"} ${closeBrace}`}
        </span>
      </button>
      {open ? (
        <div className="ml-4 border-l border-line pl-2">
          {entries.map(([k, v]) => (
            <JsonNode
              key={k}
              name={Array.isArray(value) ? undefined : k}
              value={v}
              depth={depth + 1}
              defaultDepth={defaultDepth}
            />
          ))}
          <span className="text-muted">{closeBrace}</span>
        </div>
      ) : null}
    </div>
  );
}
