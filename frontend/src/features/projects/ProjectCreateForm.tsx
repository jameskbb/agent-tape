import { useState } from "react";

import { describeError } from "@/api/client";
import { TagInput, TextAreaField, TextField, useDebounced } from "@/components/forms";
import { Button } from "@/components/ui";

import { useCreateProject, usePathCheck, type Project } from "./api";
import { PathCheckView } from "./PathCheckView";
import { formToSettings, SettingsEditor, settingsToForm } from "./SettingsEditor";

/** Add a project by path, with a live canonical-path and allowlist check. */
export function ProjectCreateForm({
  onCreated,
  onCancel,
  showSettings = true,
  submitLabel = "Add project",
}: {
  onCreated: (project: Project) => void;
  onCancel?: () => void;
  showSettings?: boolean;
  submitLabel?: string;
}) {
  const [path, setPath] = useState("");
  const [name, setName] = useState("");
  const [nameTouched, setNameTouched] = useState(false);
  const [description, setDescription] = useState("");
  const [tags, setTags] = useState<string[]>([]);
  const [settingsForm, setSettingsForm] = useState(() => settingsToForm({}));
  const [error, setError] = useState<string | null>(null);
  const debounced = useDebounced(path.trim(), 350);
  const check = usePathCheck(debounced);
  const create = useCreateProject();
  const { settings, errors } = formToSettings(settingsForm);
  const suggested = debounced.split("/").filter(Boolean).pop() ?? "";
  const effectiveName = nameTouched ? name : name || suggested;
  const pathOk = check.data?.allowed === true && check.data.input === debounced;

  return (
    <form
      className="flex flex-col gap-4"
      onSubmit={(e) => {
        e.preventDefault();
        setError(null);
        create.mutate(
          {
            name: effectiveName.trim(),
            repo_path: path.trim(),
            description: description.trim() || null,
            tags,
            settings,
          },
          { onSuccess: onCreated, onError: (err) => setError(describeError(err)) },
        );
      }}
    >
      <TextField
        label="Repository path"
        mono
        required
        value={path}
        onChange={setPath}
        placeholder="/home/you/src/my-repo"
        autoComplete="off"
        spellCheck={false}
        help="An absolute path inside one of the allowlisted project roots."
      />
      <PathCheckView
        check={debounced ? check.data : undefined}
        loading={Boolean(debounced) && check.isFetching}
      />
      <div className="grid gap-4 sm:grid-cols-2">
        <TextField
          label="Name"
          required
          value={effectiveName}
          onChange={(v) => {
            setNameTouched(true);
            setName(v);
          }}
          maxLength={200}
        />
        <TagInput label="Tags" tags={tags} onChange={setTags} />
        <TextAreaField
          className="sm:col-span-2"
          label="Description"
          rows={2}
          value={description}
          onChange={setDescription}
        />
      </div>
      {showSettings ? (
        <details className="rounded-[var(--radius-control)] border border-line px-3 py-2">
          <summary className="cursor-pointer text-sm font-medium">
            Execution settings (optional)
          </summary>
          <div className="pt-3">
            <SettingsEditor form={settingsForm} onChange={setSettingsForm} errors={errors} />
          </div>
        </details>
      ) : null}
      {error ? (
        <p role="alert" className="text-sm text-[var(--st-failed)]">
          {error}
        </p>
      ) : null}
      <div className="flex justify-end gap-2">
        {onCancel ? <Button onClick={onCancel}>Cancel</Button> : null}
        <Button
          type="submit"
          variant="primary"
          loading={create.isPending}
          disabled={!pathOk || !effectiveName.trim() || Object.keys(errors).length > 0}
        >
          {submitLabel}
        </Button>
      </div>
    </form>
  );
}
