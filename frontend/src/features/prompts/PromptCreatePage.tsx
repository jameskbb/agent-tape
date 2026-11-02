import { useState } from "react";
import { Link, useNavigate } from "react-router";

import { ApiError, describeError } from "@/api/client";
import { TagInput, TextAreaField, TextField } from "@/components/forms";
import { Page } from "@/components/layout/Page";
import { Button, Card, useToast } from "@/components/ui";

import { useCreatePrompt, type VariableDefinition } from "./api";
import { TemplateEditor } from "./TemplateEditor";
import { cleanVariables, definitionIssues } from "./variables";

const STARTER = "Summarize the recent changes in {{ area }} for the team.";

/** Describe server-side validation details in one readable line. */
export function serverIssueText(error: unknown): string {
  if (error instanceof ApiError) {
    const details = error.details;
    const undeclared = details.undeclared;
    if (Array.isArray(undeclared)) return `${error.message}: ${undeclared.join(", ")}`;
    const vars = details.variables;
    if (Array.isArray(vars)) return `${error.message}: ${vars.join(", ")}`;
  }
  return describeError(error);
}

export function PromptCreatePage() {
  const navigate = useNavigate();
  const toast = useToast();
  const create = useCreatePrompt();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [owner, setOwner] = useState("");
  const [tags, setTags] = useState<string[]>([]);
  const [body, setBody] = useState(STARTER);
  const [variables, setVariables] = useState<VariableDefinition[]>([
    {
      name: "area",
      type: "string",
      required: true,
      default: "the API",
      help: "Part of the codebase",
    },
  ]);
  const [error, setError] = useState<string | null>(null);
  const issues = definitionIssues(body, variables);

  const submit = () => {
    setError(null);
    create.mutate(
      {
        name: name.trim(),
        description: description.trim() || null,
        owner: owner.trim() || null,
        tags,
        body,
        variables: cleanVariables(variables),
        notes: "First version",
      },
      {
        onSuccess: (t) => {
          toast.show("Prompt created", "success");
          void navigate(`/prompts/${t.id}`);
        },
        onError: (e) => setError(serverIssueText(e)),
      },
    );
  };

  return (
    <Page
      title="New prompt"
      breadcrumb={
        <Link to="/prompts" className="hover:text-accent">
          Prompts
        </Link>
      }
      description="Version 1 is created when you save. Later edits become new versions."
    >
      <form
        className="flex flex-col gap-5"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <Card title="Details">
          <div className="grid gap-4 sm:grid-cols-2">
            <TextField label="Name" required value={name} onChange={setName} maxLength={200} />
            <TextField
              label="Owner"
              value={owner}
              onChange={setOwner}
              placeholder="Team or person responsible"
            />
            <TextAreaField
              className="sm:col-span-2"
              label="Description"
              rows={2}
              value={description}
              onChange={setDescription}
            />
            <div className="sm:col-span-2">
              <TagInput label="Tags" tags={tags} onChange={setTags} />
            </div>
          </div>
        </Card>
        <Card title="Template and variables">
          <TemplateEditor
            body={body}
            variables={variables}
            onBody={setBody}
            onVariables={setVariables}
          />
        </Card>
        {error ? (
          <p role="alert" className="text-sm text-[var(--st-failed)]">
            {error}
          </p>
        ) : null}
        <div className="flex justify-end gap-2">
          <Button onClick={() => void navigate("/prompts")}>Cancel</Button>
          <Button
            type="submit"
            variant="primary"
            loading={create.isPending}
            disabled={!name.trim() || !body.trim() || issues.length > 0}
          >
            Create prompt
          </Button>
        </div>
      </form>
    </Page>
  );
}
