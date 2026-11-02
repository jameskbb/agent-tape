# Claude Code (`claude-code`)

Runs Anthropic's Claude Code CLI in its documented non-interactive mode and records its
structured stream. See [ADAPTERS.md](../ADAPTERS.md) for the shared security model.

Claude Code is the adapter whose runs record findings that AgentTape checks;
[FINDINGS.md](../FINDINGS.md) explains each verdict and what it does and does not show.

- **Tested version:** Claude Code 2.1.283 (`claude --help` and the CLI reference and
  "Run Claude Code programmatically" pages, September 2026). Tests use recorded fixtures;
  no model call was made while building or testing the adapter.
- **Hooks need:** Claude Code 2.1.281 or later for the `SessionStart` command hook. That
  is the oldest version verified to run a command hook's `args` without a shell (builds
  2.1.281 to 2.1.284 were checked; older ones were not available to check and may or may
  not support it). `agenttape hooks status` warns when the installed Claude Code is older.
- **Source:** `backend/src/agenttape/adapters/builtin/claude_code.py`

## Record verified findings from Claude Code

This section takes you from nothing to a finding whose citations AgentTape has checked.
[FINDINGS.md](../FINDINGS.md) explains what each verdict shows and what it does not.

### Before you start

- AgentTape is running: `cd backend && uv run agenttape serve`.
- The repository you work in is an AgentTape project: add it on the Projects page with
  "Add project". Its path must be inside an allowed root (`AGENTTAPE_ALLOWED_ROOTS`, or
  Settings); `uv run agenttape projects check-path <path>` says whether a path is allowed
  and why not. AgentTape records nothing from a directory outside every registered
  project.
- The commands below run from `backend/`, as in the README's quick start. The reference
  sections further down write them as bare `agenttape`, the same command.

### Runs AgentTape launches

1. Create a Claude Code profile ([Profile setup](#profile-setup)).
2. Start a run from the UI, or from the command line:

   ```bash
   uv run agenttape run -p "<profile>" --prompt "<request>" --project "<project>" --follow
   ```

   (`-t "<template>"` runs a saved prompt template instead of `--prompt`.)

Nothing else is needed. Every launched run gets its own AgentTape MCP server and the
findings contract ([Findings over MCP](#findings-over-mcp)), and its findings are checked
when the run finishes. Keep `mcp__agenttape__record_finding` out of the profile's
`denied_tools`.

### Interactive sessions and Claude Code in Herdr

A session you start yourself needs two one-time registrations. Run both with the same
data dir as `serve` (the global `agenttape --data-dir <path>` option, or
`AGENTTAPE_DATA_DIR`) and the same port (set `AGENTTAPE_PORT` for all three commands:
`serve --port` alone changes only the server, and the other two would still write port
8787), and run them again after changing either: both write AgentTape's address into
Claude Code's configuration.

1. Install the hooks, which record the session's tool calls and file reads, the record
   citations are checked against:

   ```bash
   uv run agenttape hooks setup
   ```

2. Register the MCP server, which gives every Claude Code session the `record_finding`
   tool:

   ```bash
   uv run agenttape mcp register
   ```

3. Confirm both. `uv run agenttape hooks status` prints, when healthy:

   ```text
   Installed events: PreToolUse, PostToolUse, PostToolUseFailure, SessionStart, SessionEnd
   Command hook (relay): SessionStart via /path/to/agent-tape/backend/.venv/bin/python3
   Relay last run: 2026-09-29T18:00:00Z posted (HTTP 200)
   URL: http://127.0.0.1:8787/api/v1/hooks/claude-code
   Timeouts: 3, 10s
   Token valid: True
   Claude Code: 2.1.284
   ```

   and `uv run agenttape mcp status` prints:

   ```text
   Registered with Claude Code: yes
   Token valid: True
   ```

   (the port is 8787 unless you set `AGENTTAPE_PORT`).
4. Start a new Claude Code session inside the project after the setup, not a resumed one,
   and keep AgentTape running while it is open. AgentTape sees a session's reads only
   through its hooks, so it can say a cited file was never read only when the hooks
   covered the session from a fresh `SessionStart` (a new `claude`, or after `/clear`),
   with no hook event dropped and no AgentTape restart. In a session that began before
   the hooks were installed, a resumed session, an MCP-only session (no hooks), or one
   where hook events were dropped or AgentTape restarted, a citation of a file with no
   recorded read shows "Read not observed", and the citation says which gap
   ([FINDINGS.md, "Limits"](../FINDINGS.md#limits-checkable-not-foolproof)).
5. Allow `mcp__agenttape__record_finding` when Claude Code asks, or add it to
   `permissions.allow` in your Claude Code settings.
6. Ask for findings. An interactive session does not receive the findings contract a
   launched run gets; the agent sees only the tool's own description. Paste this contract
   into your request, or into your project's `CLAUDE.md`:

   ```text
   AgentTape records this run and will independently check your citations against what you actually read; for every bug, risk, recommendation, or decision you report, call the record_finding tool (from the agenttape MCP server) before your final answer, one call per finding; cite only text you read in this session; text seen only in Grep or Glob output is not a file read, so Read the file before citing it as a file citation, or cite that search as an event citation; a file citation is {path relative to the working directory, line_start, line_end, quote} where the quote is copied exactly from what you read (not paraphrased) and the line numbers are a hint; an event citation is {event_ref: the tool_use id of the command or tool call, quote: exact text from its output}; a finding with no citation is shown as "declared only".
   ```

7. Look at the result. The session appears as a run: an observed run of its own, or,
   for a Claude Code pane in Herdr, the Herdr run that hosts it. Its findings are on the
   run's Findings tab and in the Findings inbox (Source: Observed session for an observed
   run; a hosted session's findings belong to its Herdr run, a launched run). Verdicts
   appear when the run closes: for an observed run, when Claude Code sends `SessionEnd`
   or after `AGENTTAPE_OBSERVED_IDLE_TIMEOUT_SECONDS` with no hook activity (default 3600
   seconds); for a hosted session, when its Herdr run finishes. Until then the Findings
   tab says "Checked when the run finishes".

### Turning it off

- `uv run agenttape hooks remove` removes AgentTape's hook entries from Claude Code's
  settings (every other hook and key is kept, after a backup) and revokes the hooks token.
- `uv run agenttape mcp unregister` removes the AgentTape MCP server from Claude Code
  (user scope) and revokes the `mcp` token.

## Profile setup

1. Install Claude Code and sign in (or set `ANTHROPIC_API_KEY`) as described in the
   official docs. AgentTape never reads or stores those credentials.
2. `agenttape agents discover` should list `claude-code` as `installed` with its version.
   If `claude` is not on the server's `PATH`, set `AGENTTAPE_CLAUDE_EXECUTABLE`.
3. Create a profile on the Agents page (adapter "Claude Code"). With an API key, add
   `ANTHROPIC_API_KEY` to `env_allowlist`, or better, create a secret reference and map
   it with `secret_env: {"ANTHROPIC_API_KEY": "ANTHROPIC_API_KEY"}`.

## Command line

```
claude -p --output-format stream-json --verbose
       [--bare] [--mcp-config <file> --strict-mcp-config --append-system-prompt <findings contract>]
       [--model M] [--permission-mode P] [--max-turns N]
       [--allowedTools a,b] [--disallowedTools c] [--max-budget-usd X] [extra_args...]
```

The rendered prompt is written to standard input (so it never appears in `argv` or the
`command` event). The child runs in the prepared working directory with the minimal
environment.

| Profile key | Flag |
|---|---|
| `model` | `--model` |
| `permission_mode` (`default`, `acceptEdits`, `plan`, `auto`, `dontAsk`, `bypassPermissions`) | `--permission-mode` (`bypassPermissions` is allowed with a warning) |
| `max_turns` (1-500) | `--max-turns` |
| `allowed_tools` | `--allowedTools` (permission rule syntax, e.g. `Bash(git diff *)`); AgentTape appends `mcp__agenttape__record_finding` unless `denied_tools` denies it |
| `denied_tools` | `--disallowedTools` |
| `budget.max_cost_usd` | `--max-budget-usd` |
| `bare` | `--bare` (skips hooks, plugins, and CLAUDE.md/AGENTS.md discovery; recommended for scripts). The AgentTape MCP server from `--mcp-config` still loads, so `--bare` does not disable capture. |
| `extra_args` | appended; `--dangerously-skip-permissions`, `-p`, `--output-format`, `--bg`, `--worktree` and similar are refused |

## Findings over MCP

Every run AgentTape launches (`--bare` or not) automatically gets the AgentTape MCP server,
unless the worker has no reachable API address to give it (rare; a warning event is recorded
and the run proceeds without capture). Per attempt, AgentTape:

1. Mints a short-lived, per-attempt capture token and writes an owner-only (`0600`)
   `<data_dir>/capture/attempts/<attempt_id>.mcp.json` naming only the `agenttape` server
   (`type: "stdio"`, command `python -m agenttape.cli.main mcp serve`) with an `env` block
   of five variables: `AGENTTAPE_API_URL` (the loopback API address),
   `AGENTTAPE_RUN_ID`, `AGENTTAPE_ATTEMPT_ID`, `AGENTTAPE_MCP_TOKEN` (the per-attempt
   token), and `AGENTTAPE_DATA_DIR`. The token lives only in that env block of an
   owner-only file, never in `argv`, and the file is deleted when the attempt ends (step 6).
2. Passes `--mcp-config <that file> --strict-mcp-config`, so the launched session sees
   **only** the AgentTape server. A profile that needs its own MCP servers too adds its
   own `--mcp-config <file>` to `extra_args` (the flag is variadic).
3. Appends `--append-system-prompt` with a one-paragraph findings contract: call
   `record_finding` (from the `agenttape` MCP server) before the final answer, once per
   finding, citing only text actually read in the session; a file citation is `{path,
   line_start, line_end, quote}`, an event citation is `{event_ref: the tool_use id of a
   tool call, quote}`.
4. `mcp__agenttape__record_finding` is pre-approved in `--allowedTools` unless the
   profile's `denied_tools` names `mcp__agenttape__record_finding`, `mcp__agenttape`, or
   `mcp__agenttape__*`.
5. The tool's optional `target` argument (`{run_id, finding_key, outcome: upheld | refuted
   | inconclusive}`) is only for an adversarial reviewer run started by Challenge findings
   (see [Challenging a run's findings](#challenging-a-runs-findings)); ordinary findings
   omit it.
6. On every path out of `execute()` (success, failure, gated, cancelled), the token and the
   config file are removed, so a stale token is refused (401) the moment the attempt ends.

The finding itself reaches the run over one loopback HTTP call
(`POST /api/v1/mcp/findings`, see [API.md](../API.md)) into the same `EventRecorder`/
`FindingsService` path every other finding source uses; the MCP server process never opens
the database.

For unattended runs, `permission_mode: dontAsk` or `acceptEdits` with explicit
`allowed_tools` avoids permission prompts; anything that would prompt is denied and
recorded as a warning.

## Challenging a run's findings

Challenge findings (the run page button, `POST /api/v1/runs/{run_id}/challenge`, or the
CLI below) starts an ordinary child run of a reviewer profile you choose. It reads the
target run's findings and records one review finding per target finding with the
`target` argument above. Its outcomes are model judgment, never proof. The target run
must be finished (otherwise 409 `run_not_finished`) and must have recorded at least one
finding.

```
agenttape runs challenge <run> --profile <reviewer profile id or name>
       [--profile-version <version id>] [--title <title>] [--follow | -f]
       [--inline | --no-inline] [--json]
agenttape runs challenges <run> [--json]
```

`runs challenge` prints the challenge run and how many findings it reviews, how many were
left out over the reviewer prompt budget, and how many fields had delimiter-shaped text
replaced. `--follow` then streams the challenge run's events until it settles; with
`--follow`, `--inline` executes the run in this process, `--no-inline` leaves it to a
worker, and with neither flag it executes in this process only when no worker is running.
`--json` prints the start result as JSON instead of the summary lines.
`runs challenges` lists every challenge of a run (on demand and from a workflow node) with
its state, trigger, usage, cost, and each outcome labelled model judgment.

## Event mapping

| stream-json message | AgentTape event |
|---|---|
| `system` / `init` | `lifecycle` (`phase: session_started`, session id, model, cwd, permission mode, tool names) |
| `assistant` text block | `agent_message` (`role: assistant`) |
| `assistant` `tool_use` block | `tool_call` (`tool`, `input`, `tool_use_id`) |
| `user` `tool_result` block | `tool_result` (`parent_event_id` = the call, `is_error`) |
| `assistant` `thinking` / `redacted_thinking` blocks | dropped: hidden reasoning is never captured |
| `system` / `api_retry` | `warning` (error category, attempt, delay) |
| `system` / `permission_denied` | `warning` |
| `result` | `usage` event with `usage` (input, output, cache creation, cache read tokens) and `cost` (`total_cost_usd`) exactly as reported |
| `stream_event` (partial messages) | ignored |
| anything else | `agent_message` at `debug` severity with the record type |

Messages from subagents keep `subagent_of` (the parent `tool_use_id`) in the payload.
Non-JSON stdout lines and stderr become ordinary `stdout` / `stderr` events.

## Outcome

| Condition | Result |
|---|---|
| exit 0 and a `result` with `is_error: false` | `succeeded`; outputs `result`, `session_id`, `num_turns`, `subtype` |
| `result.is_error` or non-zero exit | `failed` / `adapter_error` (retryable by default) |
| an `api_retry` with `authentication_failed`, `billing_error`... | `failed` / `config_error` (not retried) |
| no `result` message | `failed` / `adapter_error` |
| wall-clock / idle limit or cancel | `timed_out` / `cancelled`; the process group is killed |

Cost comes from Claude Code's `total_cost_usd`, which the CLI documents as a client-side
estimate; AgentTape labels it that way and never computes its own.

## Interactive and Herdr sessions

A session you start yourself (in a terminal, or attached in Herdr) is not launched by
AgentTape, so there is no per-attempt token or `--mcp-config` to wire automatically. Two
one-time registrations cover it, and both are reversible.

### `agenttape hooks setup | remove | status`

`agenttape hooks setup` installs five hooks into your Claude Code `settings.json`, all
reaching `POST /api/v1/hooks/claude-code`. Four are built-in `type: "http"` hooks
(`PreToolUse`, `PostToolUse`, `PostToolUseFailure`, `SessionEnd`) with an explicit
**3 second timeout**, never Claude Code's 600s default. A hook error or timeout is
fail-open (Claude Code treats it exactly like a healthy `{}` answer), so a slow or
unreachable AgentTape never blocks a tool call.

The fifth, `SessionStart`, is a `type: "command"` hook, because Claude Code does not run
`http` hooks for `SessionStart` (or `Setup`): it skips them and logs "HTTP hooks are not
supported for SessionStart". Without a `SessionStart`, AgentTape could never show that
its hooks covered a session from its start, so "never read" would be out of reach for
every interactive and Herdr-hosted session. The entry is exec form, a `command` plus an
`args` array, which Claude Code spawns directly with no shell:

```json
{
  "type": "command",
  "command": "/path/to/agent-tape/backend/.venv/bin/python3",
  "args": ["-I", "-m", "agenttape.cli.hook_relay", "--data-dir", "/home/you/.agenttape",
           "--url", "http://127.0.0.1:8787/api/v1/hooks/claude-code"],
  "timeout": 10
}
```

`command` is the absolute interpreter AgentTape is installed in (the one that ran
`hooks setup`), and the argv is fixed: no user text and no token. Claude Code runs the
hook in the session's working directory with the session's environment, so `-I` runs
Python in isolated mode: that directory is never on Python's import path, and `PYTHON*`
variables and the user site directory are ignored, so a repository with its own
`json.py` or `logging.py` (or a `PYTHONPATH` its direnv sets) cannot run code in
AgentTape's place or print into the conversation. `agenttape.cli.hook_relay` is the
relay's own small entry point, not the whole CLI: it imports only Python's standard
library and AgentTape's token reader, so it starts quickly (about 25 ms on a warm Linux
machine, against more than a second through the CLI) and an import error elsewhere in
AgentTape cannot break it. (`agenttape hooks relay` does the same by hand.)
It reads the hook's JSON from stdin, reads the hooks token from AgentTape's owner-only token
store (`<data dir>/capture/hooks-client.token`, `0600`), and posts the JSON unchanged to
the same URL with the same headers an `http` hook sends, so AgentTape parses and records
it exactly as it would an `http` `SessionStart`. It gives up silently after 3 seconds
(inside the hook's 10 second timeout), posts only to a loopback IP address (never a host
name such as `localhost`), never uses a proxy or follows a redirect, and, once Python
has loaded it (setup's probe, below, checks that it can), always exits 0 and prints
nothing (a `SessionStart` hook's output would be added to the conversation). A new session's start therefore waits for Python to start plus at most 3
seconds; if AgentTape is down it carries on as if nothing happened.

The relay records how its last run ended in `<data dir>/capture/hook-relay-last.json`
(`0600`, and only when that directory already exists; it also removes its own
`.hook-relay-*.tmp` files over a minute old, left behind if Claude Code killed a relay
mid-write): a UTC timestamp, one of `posted`,
`rejected` (any other HTTP answer, for example 401 after a token rotation),
`unreachable`, `timeout`, `no_token`, `empty_input`, `refused_url` or `error`, and the
HTTP status when one arrived; never the token or the hook's payload. `hooks status`
shows it as "Relay last run", or "not recorded yet" when the relay has never run (for
example because Claude Code has not started a session since setup). If you move or
delete that virtualenv, run `hooks setup` again (`hooks status` warns when the
interpreter is gone). Before it writes anything, `hooks setup` runs the relay's probe
with that interpreter (`-I -m agenttape.cli.hook_relay --probe`, from an empty temporary
directory) and refuses unless it answers, so a Python that cannot import AgentTape on its
own (a system Python that only had it through `PYTHONPATH` or the user site, which `-I`
hides) is caught at setup, not at every session start.

`args` is what keeps a shell out, and a Claude Code older than 2.1.281 may not know it:
such a build would drop `args` and run `command` alone through a shell (bash, or
PowerShell on Windows). So `hooks setup` will not put an interpreter path in `command`
if it contains whitespace, a control character, a quote (typographic quotes included),
or one of `` ` $ ; & | < > ( ) { } [ ] * ? ! # % ^ , @ ``: a space, as in
`C:\Program Files\...` or `/Users/Jane Doe/...`, would split it into another command.
Letters in any script are fine (`/home/josé/...`, `C:\Users\Zoë\...`), and so is any
data dir, since the data dir only ever appears in `args`. With such an interpreter,
`setup` still installs the four `http` hooks and leaves `SessionStart` out, with a
warning that `SessionStart` is not captured (so a missing read can only ever be "Read
not observed", never "never read") and how to fix it: install AgentTape in a virtualenv
at a path without those characters and run `hooks setup` with that Python. `hooks
status` repeats the warning and reports `"session_start_captured": false`.

- **Where it writes:** the `--settings PATH` you pass, else
  `$CLAUDE_CONFIG_DIR/settings.json` when that variable is set, else
  `~/.claude/settings.json`.
- **Merge and backup:** the merge is idempotent and preserves every other hook, matcher,
  and top-level key exactly; running it again is a no-op (the token stays valid). The
  previous file is copied to `<name>.agenttape.bak` (`0600`) before every write.
- **Which entries are AgentTape's:** `setup` and `remove` replace or remove every `http`
  hook that posts to `/api/v1/hooks/claude-code` on a loopback address, whatever its port
  or token, and every command hook in the older `-m agenttape.cli.main ... hooks relay`
  form whatever data dir it names (no current AgentTape writes one, and without `-I` it
  can run modules from the session's directory). A command hook in the current form is
  this install's only when its `--data-dir` is this install's data dir: a second
  AgentTape with its own data dir (a development instance, say) keeps its `SessionStart`
  command hook, and `status` lists it for information ("Another AgentTape's relay") but
  does not count or warn about it. That instance's `http` hooks, which look the same as
  this install's apart from the port, are replaced.
- **Upgrading an older install:** an install from before the relay has an `http`
  `SessionStart` hook that Claude Code never runs (`hooks status` warns about it). Running
  `hooks setup` again replaces that one entry with the command hook, keeps the other four
  and their still-valid token, and writes the token copy the relay reads. A command hook
  from the first relay (`-m agenttape.cli.main ... hooks relay`, which loaded the whole
  CLI and, without `-I`, could run modules from the session's directory) is replaced the
  same way.
- **What each entry sends:** every `http` hook entry carries two headers,
  `Authorization: Bearer <hooks token>` and `X-AgentTape-Run-Id: ${AGENTTAPE_RUN_ID}`,
  with `"allowedEnvVars": ["AGENTTAPE_RUN_ID"]` so Claude Code may expand that one
  variable into the header. AgentTape sets `AGENTTAPE_RUN_ID` in the environment of every
  Claude Code process it launches; in a session you start yourself it is unset and the
  header is empty. The relay sends the same two headers, reading `AGENTTAPE_RUN_ID` from
  its own environment; it omits the header when the variable is unset or is not
  printable ASCII without spaces of at most 128 characters (a value AgentTape would
  discard anyway, since a run id is a UUID), and still posts the event. The header is
  what attaches a launched session's hooks to its run (see "Attached" below), and only
  when the session's working directory is inside that run's attempt directory.
- **The token:** a per-machine `hooks` token is minted only after the merge itself has
  already succeeded, so a malformed `settings.json` never leaves an orphaned token.
  Because `settings.json` then holds that token, `setup` makes the file owner-only
  (`0600`), also on a repeat run that otherwise changes nothing, and prints a line when it
  changed the mode. `setup` also writes the token to `<data dir>/capture/hooks-client.token`
  (`0600`) for the relay, and restores that copy on a repeat run if it went missing.
  `--rotate` replaces the token in both places.
- **`--dry-run`:** prints the merged `settings.json` document (the command hook included)
  with every AgentTape token masked (`Bearer atc_…`), and writes, backs up, and mints
  nothing.
- `agenttape hooks remove` removes AgentTape's hook entries, the command hook included
  (every other key and hook is kept, and the file is backed up first), and revokes the
  token, deleting the relay's copy. When the file has no AgentTape hooks (you took them
  out by hand, or pointed `--settings` at another file) it changes nothing and keeps the
  relay's copy of the token, because another settings file may still hold the relay that
  reads it and `remove` cannot know which files exist; it says so. To retire the token
  and the copy once no settings file uses AgentTape's hooks, run `hooks setup` and then
  `hooks remove`; `hooks setup --rotate` replaces the token instead.
- `agenttape hooks status [--settings PATH] [--json] [--no-probe]` reports the installed
  events, the
  hook URL, the timeouts, whether the token is still valid, and which interpreter the
  command hook runs, and warns when `disableAllHooks` is set, when an
  `allowedHttpHookUrls` list does not match AgentTape's hook URL, when the settings file
  is readable by group or others, when `SessionStart` is still an `http` hook ("is
  installed as an http hook, which Claude Code does not run"), when the command hook
  "was installed by an older AgentTape" (`-m agenttape.cli.main ... hooks relay`), when
  the command hook's interpreter no longer exists, cannot run the relay (the same probe),
  names a Python other than the one running `hooks status` (status never executes an
  interpreter it only read from a settings file, so it reports that one as not probed),
  or has a path with a character setup would refuse, when AgentTape hooks are installed
  but `SessionStart` is not captured, when the relay has no valid token copy, or, with the command hook installed, when the installed Claude Code is older
  than 2.1.281. It also prints the relay's last outcome and the installed Claude Code's
  version (found the way `agenttape agents discover` finds it), or "not found". Those two
  checks, the relay probe and `claude --version`, are the only programs status runs, and
  each is cut off after 3 seconds; `--no-probe` skips both, so status runs nothing (the
  probe result and the version are then `null`). `--json` prints `settings_path`,
  `checks_run` (`false` with `--no-probe`), `events`, `url` (`null` unless every entry uses one URL),
  `timeouts`, `token_valid`, `relay` (`events`, `interpreter`, `interpreter_exists`,
  `interpreter_runs_relay` (`null` when there is no relay, its interpreter is missing, or
  it is not the Python running `hooks status` and so was not probed),
  `token_copy_valid`, and `last_outcome`: `null`, or `at`, `outcome` and `http_status`),
  `session_start_captured` (an AgentTape command hook is installed for `SessionStart`),
  `other_relays` (each current-form relay for another data dir: `interpreter`,
  `data_dir`),
  `http_events_claude_code_skips` (AgentTape `http` hooks on an event Claude Code runs
  none for), `claude_code` (`version`, `null` when not found;
  `exec_form_min_version`, `"2.1.281"`; `exec_form_supported`, `null` when the version is
  unknown), `disable_all_hooks` (the file sets `disableAllHooks`, which turns every
  hook off, AgentTape's included), `url_blocked_by_allowlist` (the file's
  `allowedHttpHookUrls` list does not match AgentTape's hook URL, so Claude Code will
  never call it), and `warnings` (the same sentences as the text output).

### `agenttape mcp register | unregister | status`

`agenttape mcp register` first mints a per-machine `mcp` client token, or reuses the
current one while it is still valid (`--rotate` always replaces it), and then runs two
commands with an explicit argument list, never a shell:

```
claude mcp remove --scope user agenttape
claude mcp add --scope user agenttape -e AGENTTAPE_DATA_DIR=<data dir> \
       -e AGENTTAPE_API_URL=http://127.0.0.1:<port> -- <python> -m agenttape.cli.main mcp serve
```

The remove clears any earlier registration (its result is ignored); user scope means the
server applies to every project, not just the one you run it in. The token never appears
in `claude mcp add`'s argv (visible to `ps`) or in Claude Code's own `~/.claude.json`: it
reaches the server only through the owner-only (`0600`) `capture/mcp-client.token` file in
the data dir that `AGENTTAPE_DATA_DIR` names. If `claude mcp add` fails, a token this
command minted is revoked again and the command exits 1. `claude` is found on `PATH`, or
at `AGENTTAPE_CLAUDE_EXECUTABLE`.

`agenttape mcp register --print-command` prints the `claude mcp add` command that
`register` would run, and runs nothing and mints no token.

`agenttape mcp unregister` runs `claude mcp remove --scope user agenttape`, printing
"Removed the AgentTape MCP server from Claude Code (user scope)." only when that command
succeeded and otherwise what `claude mcp remove` reported, and then revokes the `mcp`
token, printing "Revoked the mcp token." or "No mcp token was present."

`agenttape mcp status [--json]` asks Claude Code whether the server is registered
(`claude mcp get agenttape`) and checks the `mcp` token. The text output is "Registered
with Claude Code: yes", "no", or "unknown", then "Token valid: True" or "False". `--json`
prints `registered` (`true`, `false`, or `null` when Claude Code is not installed or the
check could not run), `token_valid`, and `warnings` (why `registered` is `null`).

### How a session becomes a run

Once hooks are installed (for tool-level detail) or you register the MCP server (for
findings alone), a Claude Code session becomes a run the moment its first hook or
`record_finding` call arrives, in one of three ways. A session that matches none of them
is never recorded.

1. **Attached**: the session is one AgentTape itself launched. Its hooks carry the run id
   in the `X-AgentTape-Run-Id` header (from the `AGENTTAPE_RUN_ID` variable AgentTape sets
   for the run), that run is an active `claude-code` run with a running attempt, and the
   session's working directory is the attempt's working directory or inside it. The
   run-id header alone never attaches a session. Hooks only observe; stream-json already
   recorded every tool call, so nothing is duplicated. A launched run records findings
   only through its own per-attempt MCP server (above), never through the per-machine
   `mcp` token.
2. **Hosted**: the session is a Claude Code pane Herdr hosts (exactly one active Herdr
   session whose running attempt's workdir is the session's directory or contains it).
   Tool events land on that Herdr run/attempt (`source: claude-code-hook`), closing
   HERDR.md's tool-level telemetry gap for Claude.
3. **Observed**: neither of the above, and the session's directory is inside a
   **registered AgentTape project**: a new run (`kind: observed`) is created for that
   Claude Code session, `source: hook` (hooks correlated it) or `source: mcp` (an
   interactive `record_finding` call created it with no hook session in that directory).
   Outside every registered project nothing is recorded.

An observed run closes when Claude Code sends `SessionEnd`, or after
`AGENTTAPE_OBSERVED_IDLE_TIMEOUT_SECONDS` (default 3600s) of no hook activity, whichever
comes first; it is never guessed as "the agent finished".

An observed run's reads reach AgentTape only through hooks, so a citation to a file with
no recorded read is "never read" only when AgentTape can show it saw the whole session:
the first hook it recorded was `SessionStart` with source `startup` (or `clear`: after
`/clear` Claude Code starts a new session with an empty conversation), no hook event was
dropped, and AgentTape did not restart while the session was open (Claude Code's hooks
fail silently while AgentTape is down; the next hook reaching a new AgentTape process is
the only trace, recorded as a warning). Otherwise, including every `source: mcp` run and
every resumed session, the verdict is "read not observed" and its evidence names why (see
[CONTEXT-EVIDENCE.md](../CONTEXT-EVIDENCE.md)). A launched run keeps "never read": its
stream-json telemetry comes from the process AgentTape itself started. A Claude Code
session hosted in a Herdr run is read only through its hooks too, so it follows the same
rule for every session its pane runs in that run. The run's Files
tab states the same thing: reads come only from Claude Code hooks, and whether those
covered the whole session (or, for a `source: mcp` run, that no hook arrived at all).
Claude Code loads `CLAUDE.md`, its imports, and @-mentioned files without a `Read` tool
call, so no hook reports them; a citation of one of them is "never read" even on a
covered session, and "read not observed" on an uncovered one, even though the text was in
the agent's context (SECURITY.md, residual risks).

### The `session_id` fallback

`record_finding` normally resolves to the right run on its own (the hooks-correlated
session in the same directory, a session it already resolved for this MCP server process,
or a fresh observed run). When AgentTape finds more than one hook-correlated Claude Code
session in the same directory and cannot tell which one is yours, the call is refused
(409 `ambiguous_session`, nothing is recorded) with a message naming the fix: pass
`session_id` (Claude Code's own session id for that session) to `record_finding`.

A `session_id` is accepted only for a session this MCP server can speak for:

- the call reports the session's own directory or a directory below it (for a
  Herdr-hosted session, a directory inside that session's working directory). The MCP
  server reports its own working directory and AgentTape cannot verify it, so this keeps
  an honest server in another directory off the wrong run; it does not stop another
  program running as you, which can read the per-machine token and report any directory
  (see docs/SECURITY.md, residual risks);
- no other AgentTape MCP server holds the session: the server that records for a session
  keeps it while it keeps calling, and loses it only after 120 seconds with no calls, so
  a restarted MCP server (a `/mcp` reconnect or a crash) can take its own session back
  once its previous process has been quiet that long;
- AgentTape did not launch the session. A launched run records through its own
  per-attempt MCP server automatically and never needs `session_id`.

Anything else, including a `session_id` from another directory or one that names a
launched run, is refused with 409 `session_mismatch` and nothing is recorded; AgentTape
never falls back to guessing another run. A `session_id` that names no session AgentTape
is currently recording is ignored, and the call resolves as if it had none, unless
AgentTape stopped observing that session for good (its run was cancelled), which is
refused with 409 `observation_cancelled`.

## Troubleshooting capture

What an agent sees comes back from `record_finding` as the tool's error; what you see comes
from `hooks status`, `mcp status`, or the run's Findings tab. Messages are quoted as the
code prints them (a distinctive part is enough to search for).

| Symptom | Cause | Fix |
|---|---|---|
| The agent is told "AgentTape is not reachable at `<url>`; the finding was not recorded." | `agenttape serve` is not running, or it runs on a different port or data dir from the ones `mcp register` wrote into Claude Code's configuration. | Start `uv run agenttape serve`. If you changed `AGENTTAPE_PORT` or the data dir, run `uv run agenttape mcp register` (and `hooks setup`) again with the same values and restart the session. |
| "AgentTape's MCP server is not registered on this machine; run `agenttape mcp register`." | No `mcp` token in the data dir the MCP server was told to use: never registered, unregistered, or registered against another data dir. | Run `uv run agenttape mcp register` with the data dir `serve` uses. |
| "AgentTape refused this session's capture token; the finding was not recorded." | The `mcp` token was rotated or revoked, or `serve` uses a different data dir from the one the MCP server reads its token from. | Run `uv run agenttape mcp register` again with `serve`'s data dir, then reconnect the server (`/mcp`) or start a new session. |
| "AgentTape rejected this finding: `<cwd>` is not inside a registered AgentTape project; the finding was not recorded." (422 `outside_projects`) | The session's directory is outside every registered project. | Add the repository on the Projects page ("Add project"); `uv run agenttape projects check-path <path>` explains a refused path. |
| "... and cannot tell which one is yours; nothing was recorded. Pass session_id to record_finding." (409 `ambiguous_session`) | More than one hook-correlated Claude Code session is active in the same directory. | Call `record_finding` with `session_id` ([the `session_id` fallback](#the-session_id-fallback)). |
| "The Claude Code session named by session_id is not one this MCP server can record for ..." (409 `session_mismatch`) | The `session_id` names a session in another directory, a run AgentTape launched, or a session another MCP server is recording for. | Pass the id of this session, or omit `session_id` when there is only one session in the directory. |
| "AgentTape stopped observing this session; the finding was not recorded." (409 `observation_cancelled`) | The session's observed run was cancelled; AgentTape does not observe it again. | Start a new Claude Code session. |
| "AgentTape is busy recording hooks, so this finding was not recorded. Record it again in a moment." (503 `ingest_busy`) | The finding waited 5 seconds for AgentTape's hook ingest queue and never started, so nothing was written. | Record the finding again. |
| "... so it may have been recorded. Recording the identical finding again within a minute is safe: AgentTape keeps one copy." (503 `finding_write_pending`, or "AgentTape did not answer within 30 seconds") | The write started but AgentTape could not confirm it in time. | Record the identical finding again within a minute: AgentTape returns the earlier copy instead of recording a second one. After a minute, check the run's Findings tab first. |
| "AgentTape is rate-limiting capture requests right now: ... Wait a moment and try again." (429) | Too many capture requests in a short time. | Wait and record the finding again. |
| "... Split the finding into smaller findings or quote fewer lines per citation, then try again." (413) | The finding is over the request size cap. | Split the finding, or quote fewer lines per citation. |
| No run appears for an interactive session. | Hooks are not reaching AgentTape: `uv run agenttape hooks status --json` shows `token_valid` false (the token was rotated or revoked), `disable_all_hooks` true ("disableAllHooks is set in this settings file"), or `url_blocked_by_allowlist` true ("allowedHttpHookUrls is set and does not include AgentTape's hook url"). Or the session's directory is outside every registered project. Or AgentTape was not running: hooks fail open (Claude Code carries on after at most 3 seconds), so nothing was recorded. (`hooks status` also warns when the settings file "is readable by group or others": that exposes the token but does not stop recording.) | Fix what `hooks status` reports (re-run `uv run agenttape hooks setup`; `--rotate` mints a new token), register the project, keep `serve` running, and start a new session. |
| File citations on an interactive or Herdr-hosted run show "Read not observed". | AgentTape could not have seen every read of that session; the citation names the gap. `no_hooks`: "received no Claude Code hooks from this session" (an MCP-only session). `joined_mid_session`: "hooks reached AgentTape only after ... had started" (hooks installed after the session began, a resumed session, or an install from before the `SessionStart` command hook, whose `http` `SessionStart` Claude Code never runs). `dropped_hook_events`: "Claude Code hook events from this session were not recorded" (refused by the rate limit or a full ingest queue, or failed to ingest). `observer_restarted`: "AgentTape restarted while observing" (hooks sent while it was down were lost). `coverage_unknown`: "recorded before AgentTape tracked when a session's hooks began". | Install the hooks first (if `hooks status` warns that `SessionStart` "is installed as an http hook" or that the command hook's interpreter no longer exists, run `hooks setup` again), keep AgentTape running for the whole session, and start a new session (a new `claude`, or `/clear`), not a resumed one. A gap anywhere in the session counts, so the verdict for that session will not change. |
| Findings show "Checked when the run finishes". | The run has not closed yet: an observed run closes on `SessionEnd` or after the idle timeout, a hosted session's Herdr run when that run finishes. | End the Claude Code session (or wait for the idle timeout); verification then runs once, automatically. |
| `mcp register` or `mcp register --print-command` says "Claude Code (`claude`) was not found; install it or set AGENTTAPE_CLAUDE_EXECUTABLE." | `claude` is not on the `PATH` of the shell running the command. | Install Claude Code, or set `AGENTTAPE_CLAUDE_EXECUTABLE` to its path. |

## Limitations

- Credentials are not verified by discovery; the first run reports them.
- `--include-partial-messages` is not used, so text appears per message, not per token.
- Session resume (`--resume`) is not used; each attempt is a new session.
- Tool permission prompts cannot be answered from AgentTape in V1; configure permission
  mode and tool rules instead.
- Claude Code has no generic idle hook event; an observed run closes on `SessionEnd` or an
  idle-window sweep, never on "the agent went quiet".
- `--bare` only affects runs AgentTape launches (it skips hooks, plugins, and CLAUDE.md/
  AGENTS.md discovery for that one process); it has no effect on `agenttape hooks setup`,
  which is a separate, interactive-session-only registration.
- Findings recorded on an attached, hosted, or observed run appear on that run's Findings
  tab and in the Findings inbox (`/findings`).
- Directory-only session correlation has a registration race: if two Claude Code
  sessions start at the same moment in the same directory, a `record_finding` call from
  one session can bind to the other session's row, and so land on its run, while that row
  is the only matching candidate. The match is by directory, not a confirmation that it
  is that session. Likewise, any Claude Code session started inside a Herdr-hosted
  attempt's working directory is matched to that hosted run by directory alone. This
  never crosses a trust boundary on its own (a caller already holds a valid capture
  token), and every such binding is marked `cwd_sole_candidate` on AgentTape's session
  record (`details.mcp_resolved_by`, or `details.resolved_by` for a hosted match); the
  marker is not yet shown in the UI or API. The directory itself is what the caller
  reports, not a verified property of the calling process.
