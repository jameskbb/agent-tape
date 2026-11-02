# Adapters

An adapter is a thin translator between AgentTape and an agent runtime. It reports whether
the runtime is available, validates agent profiles before they are saved, runs one attempt
through the shared subprocess helper, and maps the runtime's native output to the
[event envelope](EVENT-SCHEMA.md). Adapters never touch the database; the worker hands them
an `ExecutionContext`. How to write one is in [EXTENDING.md](EXTENDING.md#add-an-agent-adapter).

| Adapter | Name | What it runs | Structured output | Usage | Cost | Setup |
|---|---|---|---|---|---|---|
| Demo Agent | `demo` | Built-in, deterministic | Yes | Simulated, labelled | No | None |
| Generic Command | `generic-command` | Any allowlisted executable | Optional (AgentTape JSONL) | If the tool emits it | No | [generic-command.md](integrations/generic-command.md) |
| Claude Code | `claude-code` | `claude -p --output-format stream-json --verbose` | Yes | As reported | CLI's own estimate | [claude-code.md](integrations/claude-code.md) |
| Codex CLI | `codex-cli` | `codex exec --json` | Yes | As reported | Not reported | [codex-cli.md](integrations/codex-cli.md) |
| Herdr runtime | `herdr` | Claude Code, Codex, or the evidence demo agent inside a Herdr pane, through Herdr's socket API | Lifecycle states; full telemetry only for the demo agent | Not reported for Claude Code / Codex | No | [HERDR.md](HERDR.md) |

Each adapter reports its runtime capabilities (start, prompt, lifecycle, events, output,
interrupt, reconnect, attach) as supported, degraded, or unsupported; the Runtime settings
page shows the table. What each adapter lets AgentTape prove about file reads and model
context is in [FILE-EVIDENCE.md](FILE-EVIDENCE.md) and
[CONTEXT-EVIDENCE.md](CONTEXT-EVIDENCE.md).

Every adapter class also declares `findings_capture`, a class attribute alongside
`capabilities`: `"mcp"` when its runs record findings through the AgentTape MCP server's
`record_finding` tool (Claude Code), `"scripted"` when a deterministic script emits
FINDING events itself (the Demo Agent), `"unsupported"` (the default, and what Codex CLI
and Generic Command report) when its runs cannot record findings at all, or
`"per_profile"` when the answer depends on which agent the profile runs (Herdr). `GET
/api/v1/adapters` carries the value verbatim as `findings` on each `AdapterInfo`.

The answer for one run comes from `findings_capture_for(config)`, which returns the class
attribute unless an adapter overrides it. Herdr answers by `agent_kind`: `claude` is
`"mcp"` (the hosted Claude Code pane's registered MCP server), `demo` is `"scripted"` (the
evidence demo agent's telemetry carries the same FINDING records a local demo run emits),
and `codex` is `"unsupported"`. Every run carries its own value as `findings_capture` on
`GET /runs` and `GET /runs/{run_id}`, resolved from the profile config the run was
snapshotted with. The Findings tab reads the run's value (falling back to the adapter's)
to explain an empty tab instead of keeping its own list of adapter names.

The Demo Agent's `finding_review` scenario is a scripted, no-model reviewer used only by
tests (never a real reviewer profile): it reads the delimited block of findings from its
own prompt, re-reads each cited file in its own private clone of the AgentTape demo
fixture (never the target run's project), and records `upheld`, `refuted`, or
`inconclusive` with its own citation per finding. Its usage is simulated and labelled
exactly like every other Demo Agent run; see [SECURITY.md](SECURITY.md) T27 and
[CONTEXT-EVIDENCE.md](CONTEXT-EVIDENCE.md) for the review contract it exercises.

## Discovery

`GET /api/v1/adapters`, `agenttape agents discover`, `agenttape doctor`, and the Agents
page all call `discover()` on every adapter. Discovery never needs credentials, never makes
a model call, and never raises (a crash is reported as `misconfigured`).

| Status | Meaning |
|---|---|
| `ready` | Built in and usable now (`demo`, `generic-command`). |
| `installed` | The CLI was found and its version read. Credentials are not checked; a run with missing credentials fails with `config_error`. |
| `misconfigured` | Found, but not executable or its version could not be read. |
| `unavailable` | Not found on `PATH` (or at the override path). |

The CLI adapters read the version from the resolved install path when the installer puts
it there (`~/.local/share/claude/versions/2.1.283`,
`~/.codex/packages/standalone/releases/0.156.1-.../bin/codex`), otherwise they run the
documented `--version` flag with a 5 second timeout. Results are cached for 60 seconds.

`AGENTTAPE_CLAUDE_EXECUTABLE` and `AGENTTAPE_CODEX_EXECUTABLE` point discovery and the
default profile executable at a specific binary.

## Security model (ADR 0005)

- **Argument arrays only.** Profiles store `executable` and `args` separately; nothing is
  ever passed through a shell. A string where an array is expected fails validation.
- **Executable allowlist.** A process adapter launches an executable only when its
  canonical path (symlinks resolved) is allowlisted. The allowlist is the union of
  `AGENTTAPE_ALLOWED_EXECUTABLES` (comma or `:` separated) and
  `<data dir>/allowed-executables.json`, which the Agents page, `PUT
  /api/v1/settings/allowed-executables`, and `agenttape executables allow|remove` edit.
  Claude Code and Codex CLI also trust their own well-known name (`claude`, `codex`) found
  on `PATH`, and the operator-set override path; choosing that adapter type is the decision
  to run that tool. The list lives in the data directory so the worker, which never reads
  the database from an adapter, re-checks it at execution time: removing an entry stops
  existing profiles from launching it (`policy_violation`).
- **Command policy (V1 safety policy).** The allowlist decides which programs may
  start; `domain/policy.check_command` decides what they may be asked to do. Before
  anything is spawned, every process adapter judges the agent command and each
  preflight and validation command (`adapters/builtin/_command_policy.py`). Push, merge,
  pull, rebase, force push, destructive reset/clean/checkout, branch/tag/worktree
  deletion, shells, `sudo`, `rm`, and environment dumps are refused with
  `policy_violation` even with an approval. Git global options (`-C`, `--git-dir`,
  `-c alias.*`, `--config-env`, `--exec-path`), repository aliases (resolved; shell
  aliases refused), combined short flags (`-fu`), and `git-<sub>` helper binaries are all
  seen through. `git commit`, git subcommands not known to be read-only, `git -c`
  settings, config writes, and profiles with `"elevated": true` pause the run for an
  approval of the exact (fingerprinted) command list. `GIT_*`, `LD_PRELOAD`, `DYLD_*`,
  and `BASH_ENV` are never passed to children. **Limitation:** the policy sees argv, not
  behaviour; an allowlisted interpreter, build tool, or wrapper script (or Claude Code or
  Codex themselves) can run anything internally. See docs/SECURITY.md (T7, T8).
- **Validation before save.** `AgentProfileService.validate_config` runs the adapter's
  shape checks plus local policy (`validate_with_policy`): executable exists, is
  executable, and is allowlisted; working directory mode; argument arrays; environment
  names; secret reference names; timeouts; attempts; budget keys; tool rules; permission
  modes. Errors block saving; warnings (for example `bypassPermissions`) are shown.
  `POST /api/v1/agent-profiles/validate` runs the same checks without saving.
- **Minimal environment.** Children get a safe base (`PATH`, `HOME`, `LANG`, `TZ`,
  `TMPDIR`, `USER`...), the names in `env_allowlist`, `AGENTTAPE_RUN_ID`,
  `AGENTTAPE_ATTEMPT`, and `secret_env`. Other host variables are not passed.
- **Secrets by reference.** `secret_env` maps an environment name to a secret reference
  name (`{"ANTHROPIC_API_KEY": "ANTHROPIC_API_KEY"}`). The value is read from the process
  environment (or the reference's locator) only when the attempt starts, registered with the
  redactor, and never stored. Secret reference metadata is managed at
  `/api/v1/secret-references`; the API reports only whether the variable is set.
- **Working directory.** `workdir_mode: project` (default) runs in the directory the worker
  prepared (the project repository, a Git worktree when isolation is on, or a scratch
  directory). `subdirectory` runs in `working_dir`, a relative path that cannot escape it.
- **Cancellation and timeouts.** `run_process` runs the child in its own process group,
  kills the whole group on cancel or timeout (SIGTERM, then SIGKILL), and drains both pipes
  concurrently with bounded memory.
- **Unsafe flags refused.** `extra_args` may not contain flags that disable the safety
  posture (`--dangerously-skip-permissions`, `--dangerously-bypass-approvals-and-sandbox`)
  or that break the output contract (`--output-format`, `--json`...).

## Profile fields

Common keys understood by the engine or every process adapter:

| Key | Type | Notes |
|---|---|---|
| `executable` | string | Absolute path or name on `PATH`; must be allowlisted |
| `args` | string[] | Generic Command only; CLI adapters build their own argv and accept `extra_args` |
| `model` | string | Passed to CLIs that support model selection |
| `workdir_mode`, `working_dir` | string | See above |
| `env_allowlist` | string[] | Host variable names passed through |
| `secret_env` | object | `{ENV_NAME: SECRET_REFERENCE_NAME}` |
| `timeout_seconds`, `idle_timeout_seconds` | number | Wall-clock and no-output limits |
| `max_attempts`, `retry` | int, object | Automatic retry of retryable failures |
| `permissions` / `permission_mode` / `sandbox` | string | Adapter specific |
| `allowed_tools`, `denied_tools` | string[] | Claude Code only (warning elsewhere) |
| `budget` | object | `max_cost_usd`, `max_*_tokens`, `max_changed_files`, `max_diff_lines`, `max_artifact_bytes` |
| `preflight_commands`, `validation_commands` | string[][] | Allowlisted argv arrays run before / after the agent; a non-zero exit is `validation_failed` |
| `evaluators` | object[] | Evaluators run after each attempt |

A profile is versioned: saving a changed configuration creates a new
`AgentProfileVersion`; runs snapshot the version they used, so later edits never rewrite
history. Metadata (name, description, tags, archived) is edited with `PATCH
/api/v1/agent-profiles/{id}`; archived profiles cannot start runs.

## Usage and cost

Adapters report usage and cost only when the tool does. `usage` keys are copied verbatim
(`input_tokens`, `output_tokens`, cache counts...). Cost uses
`{"total_usd": <number>, "currency": "USD", "source": "<where it came from>"}`. Claude
Code's `total_cost_usd` is a client-side estimate and is labelled as such; Codex reports no
cost, so the UI shows "unavailable".

## Testing

Adapters are tested without real model calls:

- `backend/tests/fixtures/adapters/*.jsonl` are recorded streams written from the
  documented formats (Claude Code stream-json, Codex `exec --json`).
- `tests/unit/test_cli_adapters_normalize.py` checks the event mapping, parent links,
  usage and cost extraction, and that hidden reasoning (`thinking` blocks) is dropped.
- `tests/integration/test_process_adapters.py` runs each adapter through the real worker
  against small fake executables that replay those fixtures, and covers allowlist
  enforcement, secret redaction, preflight/validation commands, timeout, and cancellation.
- `tests/conftest.py` points `AGENTTAPE_CLAUDE_EXECUTABLE` and
  `AGENTTAPE_CODEX_EXECUTABLE` at a missing path for every test, so the suite can never
  discover or launch the real CLIs even when they are installed.
- `tests/integration/test_finding_review.py` and `test_finding_review_injection.py` drive
  the Demo Agent's `finding_review` scenario through the real API and worker; no test
  calls a model, so they prove prompt construction and scripted disagreement, never a real
  model's resistance to prompt injection.
