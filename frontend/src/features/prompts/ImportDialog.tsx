import { useState } from "react";
import { useNavigate } from "react-router";

import { describeError } from "@/api/client";
import { SelectField, TextAreaField } from "@/components/forms";
import { Modal } from "@/components/forms/Modal";
import { Button, useToast } from "@/components/ui";

import { useImportPrompts } from "./api";

/** Import prompts from an AgentTape export file (or pasted JSON). Nothing is overwritten. */
export function ImportDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [text, setText] = useState("");
  const [onConflict, setOnConflict] = useState<"rename" | "skip">("rename");
  const [error, setError] = useState<string | null>(null);
  const importer = useImportPrompts();
  const toast = useToast();
  const navigate = useNavigate();

  const submit = () => {
    let bundle: unknown;
    try {
      bundle = JSON.parse(text);
    } catch {
      setError("This is not valid JSON. Choose a file exported from AgentTape.");
      return;
    }
    setError(null);
    importer.mutate(
      { bundle, onConflict },
      {
        onSuccess: (result) => {
          const n = result.created.length;
          toast.show(
            `Imported ${n} prompt${n === 1 ? "" : "s"}` +
              (result.skipped.length ? `, skipped ${result.skipped.length}` : ""),
            "success",
          );
          setText("");
          onClose();
          const first = result.created[0];
          if (n === 1 && first) void navigate(`/prompts/${first.id}`);
        },
        onError: (e) => setError(describeError(e)),
      },
    );
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      wide
      title="Import prompts"
      description="Import a file exported from AgentTape. Every version is recreated in order; existing prompts are never overwritten."
    >
      <form
        className="flex flex-col gap-4"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <label className="flex flex-col gap-1 text-sm">
          <span className="font-medium text-ink-soft">Export file</span>
          <input
            type="file"
            accept="application/json,.json"
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (!file) return;
              void file.text().then(setText);
            }}
            className="text-sm text-ink-soft file:mr-3 file:rounded-[var(--radius-control)] file:border file:border-line-strong file:bg-surface file:px-3 file:py-1.5 file:text-ink"
          />
        </label>
        <TextAreaField
          label="Or paste JSON"
          mono
          rows={8}
          value={text}
          onChange={setText}
          placeholder='{"format": "agenttape.prompts", "templates": [...]}'
          error={error}
        />
        <SelectField
          label="When a prompt with the same name exists"
          value={onConflict}
          onChange={(v) => setOnConflict(v === "skip" ? "skip" : "rename")}
          options={[
            { value: "rename", label: "Import it under a new name" },
            { value: "skip", label: "Skip it" },
          ]}
        />
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button
            type="submit"
            variant="primary"
            loading={importer.isPending}
            disabled={!text.trim()}
          >
            Import
          </Button>
        </div>
      </form>
    </Modal>
  );
}
