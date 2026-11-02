# Codex CLI (`codex-cli`)

Runs OpenAI's Codex CLI in its documented non-interactive mode (`codex exec --json`) and
records its JSONL event stream. See [ADAPTERS.md](../ADAPTERS.md) for the shared security
model.

- **Tested version:** codex-cli 0.156.1 (`codex exec --help` and the non-interactive mode
  docs, September 2026). Tests use recorded fixtures; no model call was made.
- **Source:** `backend/src/agenttape/adapters/builtin/codex_cli.py`

## Setup

1. Install the Codex CLI and sign in as its docs describe. AgentTape never reads or
   stores those credentials.
2. `agenttape agents discover` should list `codex-cli` as `installed`. If `codex` is not
   on the server's `PATH`, set `AGENTTAPE_CODEX_EXECUTABLE`.
3. Create a profile on the Agents page (adapter "Codex CLI"). Codex reads its login from
   `CODEX_HOME` (default `~/.codex`) under `HOME`, which the minimal environment keeps; add
   `OPENAI_API_KEY` through `secret_env` if you use an API key.

## Command line

```
codex exec --json --color never [--model M] [--sandbox S] [--skip-git-repo-check]
           [--ephemeral] --cd <workdir> [extra_args...] -
```

The trailing `-` makes Codex read the rendered prompt from standard input.

| Profile key | Flag |
|---|---|
| `model` | `--model` |
| `sandbox` (`read-only`, `workspace-write`, `danger-full-access`) | `--sandbox` (`danger-full-access` is allowed with a warning) |
| `skip_git_repo_check` | `--skip-git-repo-check` (needed outside a Git repository) |
| `ephemeral` | `--ephemeral` |
| `extra_args` | appended; `--dangerously-bypass-approvals-and-sandbox`, `--json`, `--worktree`, `-o` are refused |

`allowed_tools`, `denied_tools`, and `max_turns` have no Codex equivalent; setting them
produces a validation warning.

## Event mapping

| Codex JSONL event | AgentTape event |
|---|---|
| `thread.started` | `lifecycle` (`phase: session_started`, `thread_id`) |
| `turn.started` | ignored |
| `item.completed` `agent_message` | `agent_message` |
| `item.completed` `reasoning` | `reasoning_summary` (the summary text Codex itself emits; `provided_by: codex-cli reasoning item`) |
| `item.started` `command_execution` | `tool_call` (`tool: shell`, `command`) |
| `item.completed` `command_execution` | `tool_result` (`aggregated_output`, `exit_code`, `status`; error when failed, declined, or non-zero) |
| `item.*` `mcp_tool_call` | `tool_call` (`server.tool`, arguments) then `tool_result` (result or error) |
| `item.completed` `web_search` | `tool_call` (`tool: web_search`) |
| `item.completed` `file_change` | one `file_write` per changed path (`operation`: add, update, delete) |
| `item.*` `todo_list` | `agent_message` (`kind: plan`, checklist text) |
| `item.completed` `error` | `warning` |
| `turn.completed` | `usage` (`input_tokens`, `cached_input_tokens`, `output_tokens`, `reasoning_output_tokens`, summed across turns) |
| `turn.failed`, `error` | `error` |

## Outcome

| Condition | Result |
|---|---|
| exit 0 and no `turn.failed` / `error` | `succeeded`; outputs `thread_id`, `last_message` |
| `turn.failed`, `error`, or non-zero exit | `failed` / `adapter_error` |
| wall-clock / idle limit or cancel | `timed_out` / `cancelled`; the process group is killed |

Codex does not report cost, so run cost stays unavailable.

## Limitations

- The docs publish the event categories and an example stream but not an exhaustive schema
  for every item type; unrecognised items are recorded at `debug` severity rather than
  dropped.
- Credentials are not verified by discovery; the first run reports them.
- `codex exec resume` is not used; each attempt starts a new thread.
