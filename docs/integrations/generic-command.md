# Generic Command (`generic-command`)

Runs any allowlisted local executable with an argument array. Use it for scripted agents,
in-house tools, or CLIs AgentTape has no dedicated adapter for. See
[ADAPTERS.md](../ADAPTERS.md) for the shared security model.

- **Version:** built in (`1`). **Source:** `backend/src/agenttape/adapters/builtin/generic_command.py`

## Setup

1. Allowlist the executable: Agents page > Executable allowlist, `agenttape executables
   allow /usr/local/bin/my-agent`, or `AGENTTAPE_ALLOWED_EXECUTABLES`.
2. Create a profile with adapter "Generic Command", the executable, and its arguments one
   per row. Arguments are passed exactly as stored; there is no shell, no globbing, and no
   variable interpolation.

## Profile keys

| Key | Default | Meaning |
|---|---|---|
| `executable` | required | Allowlisted absolute path or name on `PATH` |
| `args` | `[]` | Argument array |
| `prompt_mode` | `stdin` | `stdin`: prompt on standard input; `env`: `AGENTTAPE_PROMPT`; `arg`: appended as the final argument; `none` |
| `parser` | `text` | `text` or `jsonl` (below) |
| `success_exit_codes` | `[0]` | Exit codes that count as success |
| common keys | | `workdir_mode`, `working_dir`, `env_allowlist`, `secret_env`, timeouts, retries, `preflight_commands`, `validation_commands` ([ADAPTERS.md](../ADAPTERS.md#profile-fields)) |

The child also receives `AGENTTAPE_RUN_ID` and `AGENTTAPE_ATTEMPT`. With `prompt_mode:
arg`, secret values resolved into the prompt appear in the process arguments; they are
redacted from events and logs, but prefer `stdin` for prompts that reference secrets.

## Output

With `parser: text`, stdout and stderr lines become `stdout` / `stderr` events and full
redacted logs are kept under `logs/<run>/attempt-N.*.log`.

With `parser: jsonl`, a stdout line that is a JSON object with `"agenttape": 1` becomes a
structured event; every other line stays plain stdout:

```json
{"agenttape": 1, "type": "agent_message", "summary": "Looking at the tests", "payload": {"role": "assistant", "text": "Looking at the tests"}}
{"agenttape": 1, "type": "tool_call", "summary": "grep(TODO)", "payload": {"tool": "grep", "input": {"pattern": "TODO"}}}
{"agenttape": 1, "type": "file_write", "summary": "Wrote src/app.py", "payload": {"path": "src/app.py", "bytes": 120}}
{"agenttape": 1, "type": "usage", "payload": {"input_tokens": 1200, "output_tokens": 300}}
```

Allowed `type` values: `agent_message`, `reasoning_summary` (only a summary your tool
chooses to publish), `tool_call`, `tool_result`, `file_read`, `file_write`, `usage`,
`warning`. Optional `severity` is one of the envelope severities. Numeric `usage` payload
fields become the event's and run's usage exactly as reported. Other types are recorded as
a warning, never trusted as lifecycle or approval events.

## Outcome

| Condition | Result |
|---|---|
| exit code in `success_exit_codes` and validation commands pass | `succeeded` |
| other exit code | `failed` / `adapter_error` (retryable by default) |
| executable missing | `failed` / `adapter_unavailable` |
| executable no longer allowlisted | `failed` / `policy_violation` (nothing is launched) |
| the command, a preflight, or a validation command is hard-disallowed (`git push`, `reset --hard`, `branch -D`, `rm`, a shell...) | `failed` / `policy_violation` before anything is launched ([ADAPTERS.md](../ADAPTERS.md#security-model-adr-0005)) |
| a command needs approval (`git commit`, an unknown git subcommand, `"elevated": true`) | `waiting_for_approval`; runs after approval, fails on rejection |
| a preflight or validation command fails | `failed` / `validation_failed` |
| wall-clock / idle limit or cancel | `timed_out` / `cancelled`; the process group is killed |

## Limitations

- Cost is never reported by this adapter.
- No interactive input: standard input carries the prompt (or nothing) and is then closed.
