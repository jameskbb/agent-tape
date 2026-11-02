import type { UseQueryResult } from "@tanstack/react-query";
import { KeyRound } from "lucide-react";

import { useDebounced } from "@/components/forms";
import { Badge, CodeBlock, ErrorState, Skeleton } from "@/components/ui";

import { useRenderPreview, type RenderPreview } from "./api";

/** Debounced live preview for a template version and the current form inputs. */
export function useLivePreview(
  templateId: string | null,
  versionId: string | null,
  inputs: Record<string, unknown>,
) {
  const debounced = useDebounced(inputs, 250);
  return useRenderPreview(templateId, versionId, debounced);
}

/** Rendered prompt text, secret references, and variable problems. */
export function RenderedPreview({ preview }: { preview: UseQueryResult<RenderPreview> }) {
  if (preview.isPending) return <Skeleton lines={4} />;
  if (preview.isError) return <ErrorState error={preview.error} title="Preview failed" />;
  const { text, secret_refs: secrets, issues, valid } = preview.data;
  return (
    <div className="flex flex-col gap-2" aria-live="polite">
      <div className="flex flex-wrap items-center gap-2">
        {valid ? (
          <Badge tone="success">Ready to run</Badge>
        ) : (
          <Badge tone="danger">
            {issues.length} problem{issues.length === 1 ? "" : "s"}
          </Badge>
        )}
        {secrets.map((s) => (
          <Badge
            key={s}
            tone="violet"
            title="Resolved from the environment only when the run executes"
          >
            <KeyRound aria-hidden className="size-3" />
            {s}
          </Badge>
        ))}
      </div>
      <CodeBlock code={text} label="Rendered prompt (as stored with the run)" />
      {issues.length ? (
        <ul className="flex flex-col gap-0.5 text-sm text-[var(--st-failed)]">
          {issues.map((i, k) => (
            <li key={k}>
              <code>{i.variable}</code> {i.message}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
