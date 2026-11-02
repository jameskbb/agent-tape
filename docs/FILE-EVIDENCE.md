# File Evidence

File Evidence answers, for every run: which project files did it read, create, modify,
rename, delete, restore, or generate; when; which attempt, step, tool, or command caused
it; what evidence supports that; what the file looked like before and after; whether it
ended with a net Git change; whether it touched a protected path or left the project
boundary; and how complete that evidence is.

A final `git diff` is not a touched-file record. A file edited several times and then
restored has no diff, but it still appears in the run's **Files** tab as *Restored* with a
*Touched, net unchanged* badge.

Code: `backend/src/agenttape/file_evidence/` (`ledger.py` holds every rule and is pure;
`collector.py` the worker hooks; `snapshot.py` the Git snapshots; `store.py` persistence and
rebuilds; `service.py` queries and exports), the router `api/routers/file_evidence.py`,
the UI in `frontend/src/features/files/`. Tables: migrations `m2B_0001`, `m2B_0002`.

## Evidence layers

| Layer | Source values | What it proves | What it cannot prove |
|---|---|---|---|
| Agent and tool telemetry | `agent_hook` (file_read / file_write / file_search / file_rename records), `tool_event` (tool calls with a path argument), `command_reference` (a command argument naming an existing file) | Intent, and reads **only when the agent or tool says so** | That the telemetry is complete; a command argument is a reference, not a read |
| Live filesystem observation | `filesystem_watcher` | That a path was created, changed, or deleted while the attempt ran | Reads (never labelled as reads); which process made the change |
| Git and content reconciliation | `git_end`, `reconciliation` | The net state at the end versus the start (content hashes), changes the watcher missed, exact renames | What happened in between |

Raw evidence is append-only (`file_evidence_events`), with one exception: a later
reconciliation of the same attempt replaces that attempt's earlier `git_end` and
`reconciliation` rows (a pause for approval reconciles provisionally, and the resumed
attempt may revert the change). The per-file ledger (`run_file_summaries`) is derived and
rebuilt deterministically from the raw evidence plus the start and end snapshots
(`file_evidence_snapshots`); rebuilding never deletes evidence.

## Terms (user-facing labels)

| Classification | Label | Meaning |
|---|---|---|
| `observed_read` | Observed read | An agent or tool explicitly reported reading the file, including a bounded read-only shell command (`cat`/`head`/`tail`/`sed -n`) recognized as a read (source `tool_event`, quality `reported`, `shell_read` details naming the program and the line range) |
| `inferred_access` | Inferred access | A search result, tool, or command referenced the path; a read is not proven |
| `created` | Created | Did not exist at run start and exists at the end |
| `modified` | Modified | Content differs between start and end |
| `renamed` | Renamed | Evidence links the old and new paths (rename chains are merged into one logical file) |
| `deleted` | Deleted | Existed at start and is gone |
| `restored` | Restored | Changed during the run (an intermediate content hash differed, or the agent reported a write that the watcher confirmed) and ended with its original content hash |
| `net_unchanged` | Net unchanged | Activity was observed but the final state matches the start (created then deleted; rewritten with identical bytes) |
| `generated` | Generated | Created or changed only by an identified command category (formatter, build, test, install, generate) with no agent write report |
| `outside_boundary` | Outside boundary attempt | A declared operation named a path outside the run's permitted root |
| `unknown` | Unknown | Evidence exists but is not strong enough (for example the agent reported a write that neither the watcher nor Git saw, or the starting state of a Git-ignored path was not captured) |

*Touched, net unchanged* is a badge (`net_unchanged`) on restored and net-unchanged files.
Net Git status is one of `added`, `modified`, `deleted`, `renamed`, `unchanged`,
`unknown`, or `pending` (the attempt has not been reconciled yet).

Every ledger row carries an `explanation` ("why AgentTape believes this file was
touched"), its evidence sources, operation counts (each operation once per file, from the
evidence source that saw it most often: one write reported by the agent, seen by the
watcher, and found again by Git counts once), first and last activity, attempts,
agents, tools and commands, step, start and end hashes, diff statistics, protected-path
patterns and the live policy decision, and a per-file completeness value (`observed`,
`reconciled_only`, `telemetry_only`, `inferred`, `pending`).

## Collection and reconciliation

The evidence boundary is the run's working directory: the isolated worktree when the
project uses Git isolation, else the project repository (or a scratch directory).

1. **Before the agent starts** (the agent executor awaits `start_file_evidence`, bounded
   wait): the collector takes the **start snapshot** and brings the watcher up; only then does the
   adapter execute, so no early change is missed or baked into the "start" state. The
   start snapshot records the commit, branch, dirty state, Git's own blob ids for every
   tracked file (`git ls-files -s`, no file is read), sizes, hashes of untracked
   non-ignored files, and the list of ignored entries without recursing into them.
   A resumed attempt (after an approval) keeps its original start snapshot.
2. **While the attempt runs** (attempt monitor): about once a second it turns new
   telemetry events into evidence (idempotent: keyed by source event id) and flushes the
   watcher's batches. Watcher changes are debounced (300 ms), hashed as soon as a batch
   arrives (so an intermediate state is evidence of a restore), coalesced per path per
   flush with first/last timestamps and counts, and paired into renames by content hash
   (a vanished path's last known hash against a new path's hash; a file created in one
   watcher batch and renamed in a later one within the same flush is recorded as a create
   followed by the rename). Temporary-save patterns (`*.tmp`, `.*.swp`, `*~`, ...) created
   and removed next to a target in one batch are folded into the target's history.
   Summaries are rebuilt live about every two seconds (net status `pending`); a rebuild
   reads the whole ledger, so for large ledgers the interval grows to keep rebuilds under
   a fifth of the time.
3. **After the attempt** (after-attempt hook, order 55): catch up on telemetry, take the
   **end snapshot** (paths with evidence, paths Git reports as changed against the start
   commit, untracked files, numstat, which paths Git ignores), then record a `git_end`
   event per net change and a `reconciliation` finding for every change the watcher did
   not report (`changed_without_watcher_event`) and for exact renames found only by
   content hash, then rebuild. This also runs when the attempt pauses for approval
   (provisional; the completeness statement says so) and again when it finishes.

Idempotency: every evidence row has a per-run unique `dedupe_key` and a per-run unique
`sequence`; Git end and reconciliation keys include the attempt and the final content
hash, so a retry, a restart, or a manual reconcile never duplicates rows.

Attribution is to the narrowest reliable scope: run and attempt always; the workflow step
only when the run is a workflow node child with its own isolated worktree (otherwise the
run's completeness says `step_attribution: unavailable`); the tool call (tool name and
`tool_use_id`) and command (argument array) when telemetry names them; watcher activity
during a command's declared start/finish window is attributed to that command. Nothing
is guessed.

## Evidence completeness

Each run shows one statement, built from what actually ran:

* "Writes and final changes fully observed; reads available only from agent telemetry."
* "Filesystem observation unavailable; final Git reconciliation completed."
* "Agent telemetry unavailable; read activity not captured."
* On an observed Claude Code session (one AgentTape did not start), or one hosted in a
  Herdr run, reads come only from its Claude Code hooks (even a covered session that read
  nothing has them): "reads available only from Claude Code hooks, which AgentTape
  received for the whole session.", or "which did not cover the whole session." followed
  by the gaps (joined mid-session, dropped hooks, AgentTape restarted), or, for a session
  that only recorded findings over MCP, "AgentTape received no Claude Code hooks from
  this session, so it saw none of its reads." These are the same gaps that make a missing
  read `read_not_observed` rather than `never_read` (CONTEXT-EVIDENCE.md).
* "Partial: AgentTape was offline from X to Y; final Git reconciliation still ran." (a
  runtime reconnect event marks the window; nothing is fabricated for it)
* "Writes observed so far; Git reconciliation is provisional while the run waits for
  approval."

Observation is *partial* when the watcher degraded (more than 5,000 distinct paths
pending in one flush: the excess is counted as dropped, never silently lost), failed, or
never stopped cleanly (the worker died). Run summary counts are `null` (shown as
unavailable, never 0) when the data does not exist (for example reads for an agent whose
telemetry does not report them).

## Noise policy

* `.git` internals are excluded entirely.
* Collapsed by default (counted per directory, never hashed or read, one evidence row per
  directory per flush): `node_modules`, `bower_components`, `.venv`, `venv`,
  `__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `.tox`, `.nox`, `.cache`,
  `.gradle`, `.next`, `.nuxt`, `.turbo`, `.parcel-cache`, `build`, `dist`, `target`,
  `coverage`, `htmlcov`, `.nyc_output`, `.agenttape`, and files `*.pyc`, `*.pyo`,
  `.coverage*`, `.DS_Store`. The ledger shows them as rows such as "12,431 filesystem
  events under node_modules/, collapsed by the noise policy".
* Everything else (source, configuration, migrations, tests, docs, untracked project
  files) is tracked individually.

Per-project settings (`settings.file_evidence` on the project):

| Key | Default | Meaning |
|---|---|---|
| `collapse` | `[]` | Extra globs to collapse (e.g. `["logs/", "*.log"]`) |
| `capture` | `[]` | Globs always tracked individually, overriding the defaults (e.g. `["dist/manifest.json"]`) |
| `watcher` | `true` | `false` turns live observation off (telemetry and Git reconciliation still run) |
| `cancel_on_protected_write` | `false` | A detected protected-path write is always a high-severity event and a review-required finding; `true` also stops the attempt (`policy_violation`) |
| `cancel_on_outside_write` | `true` | A declared write outside the boundary is always a high-severity event; `true` also stops the attempt (`policy_violation`) |

## Protected paths and the boundary

Protected paths are the project's existing `protected_paths` setting, matched with
`domain/policy.matches_protected`.

* **Before the operation** (telemetry can pause): an agent that declares its intent (the
  demo agent's `approval_request`) goes through the normal approval flow; the run waits
  for approval, and the later write is recorded with `decision: approved` and the
  approval id.
* **After the operation** (the watcher or a write report saw it): AgentTape records the
  policy decision on the evidence (`decision: detected`, `prevented: false`), emits a
  high-severity `file_evidence` event ("detected, not prevented"), marks the finding
  review-required, and stops the attempt when `cancel_on_protected_write` is set. It never
  claims a detected write was prevented. A protected file that was only read or searched
  is shown as a protected path, without that wording.
* **Outside the boundary**: a declared operation naming a path outside the run's root is
  recorded with `inside_boundary: false`; a write triggers a high-severity event and,
  unless `cancel_on_outside_write` is `false`, stops the attempt. AgentTape never reads,
  stats, or
  hashes such a path; paths are normalized lexically, so `..` cannot resolve inside.
  Command arguments are only ever matched against files inside the root.
* The final Git check always runs, even when live observation reported nothing.

## Privacy

* The File Evidence tables hold metadata and hashes only: no file contents are stored in
  them, and binaries are flagged, never stored. File content the agent read is kept
  elsewhere for verified findings (retained reads and `tool_result` events, redacted and
  capped; SECURITY.md T16). Hashes are Git blob ids (SHA-1 over the content), which
  cannot be reversed but do reveal whether a file equals a known version.
* Command text and structured details come from the run's events, which the executor
  already redacted (configured secret values and credential patterns); they pass through
  the pattern redactor again before they are stored.
* Exports (`/runs/{id}/files/export`, `/runs/{id}/evidence/export`) are redacted again with
  the export redactor, use repository-relative paths, and reduce outside-boundary paths to
  `<outside-boundary>/<file name>`; absolute paths inside the boundary are never stored.
  Truncated
  data is marked (`truncated`, `evidence_events_truncated`).
* Evidence rows cascade with their run, so retention and `reset-demo` remove them.

## What AgentTape can and cannot prove about reads

| Adapter / runtime | Reads | Writes | Notes |
|---|---|---|---|
| `demo` (Demo Agent, including the evidence demo agent `python -m agenttape.demo_agent`) | Observed: explicit `file_read` records and `Read` tool calls; searches are inferred access | Reported writes and renames, confirmed by the watcher and Git | Deterministic and scripted; the same telemetry under Herdr |
| `claude-code` | Observed when the `Read`/`NotebookRead` tool is used (stream-json tool events); `Grep`/`Glob` are inferred access; a `Bash` call that is a bounded read (`cat` or `cat -n` of up to 16 files; `head -n N`, `-nN`, `-N`, or `--lines=N`; `tail -n N` or `-n +N` (also `-N` and `--lines=`); `sed -n 'Np'`, `'N,Mp'`, or `'N,$p'`; each optionally as `/bin/<program>` or `/usr/bin/<program>`, with no pipes, redirects, substitutions, variables, globs, env assignments, or any other option) is also observed, as a read | `Write`/`Edit`/`MultiEdit` tool events plus watcher and Git | Every other `Bash` command is not captured as a read: the command is parsed, never re-run, and anything outside that bounded grammar stays a reference at most |
| `codex-cli` | Not reported by Codex's JSON events: reads are unavailable | `file_change` items plus watcher and Git | Shell commands are single strings; they are not parsed into paths |
| `generic-command` | Only if the command emits `file_read` JSONL records | Emitted records, watcher, Git | Argument arrays are checked for file references (inferred access) |
| `herdr` runtime | Whatever telemetry the Herdr adapter can read: identical to `demo` for the evidence demo agent; for an interactive agent without a telemetry file, reads are unavailable | Watcher and Git on the run's workdir (plus telemetry if available) | If AgentTape was offline, the window is marked and only Git reconciliation covers it |

| Platform | Watcher backend | Notes |
|---|---|---|
| Linux | inotify (via `watchfiles` / notify) | Create, modify, delete; a rename arrives as delete + create and is paired by content hash. Large trees need `fs.inotify.max_user_watches`; if the watcher cannot start, evidence says "Filesystem observation unavailable" and Git reconciliation still runs. No read events are used. |
| macOS | FSEvents | Events are coalesced and can arrive late or at directory granularity; Git reconciliation catches anything missed. No read events. |
| Windows | Run AgentTape in WSL (POSIX only) | In WSL2, inotify works on the Linux filesystem. Files under `/mnt/c` (drvfs/9P) edited by Windows processes do **not** raise inotify events: live observation is effectively unavailable there, and only Git reconciliation proves final changes. Keep repositories on the Linux filesystem. |

Metadata-only changes (permissions, timestamps) are not distinguished from content
changes by the watcher; a hash comparison decides whether content changed.

## API

See [API.md](API.md#file-evidence): paginated run ledger with filters equivalent to the UI,
summary counts and completeness, a per-file timeline (event trail, start/end state, final
diff, linked events and approvals), sanitized CSV/JSON ledger export, the sanitized
evidence bundle (JSON, or CSV of the raw evidence), an audited rebuild/reconcile, and
project file activity (most touched, failed-run files, changed together, protected
activity, recent activity, one file's run history; every entry links to its runs).

The evidence bundle includes the completeness statement, the ledger, the redacted raw
evidence, start and end Git metadata, final diff statistics, collapsed-directory
summaries, protected and boundary findings, runtime references from `runtime_sessions`
(an explicit list of public columns: runtime, machine, workspace/tab/pane ids, agent name
and kind, lifecycle state, Herdr version and protocol, status; never sockets or native
session values), and the context summary from `agenttape.context_evidence.export`.

## Demo

`agenttape seed` creates the project "AgentTape Evidence Demo" (Git isolation; protected
`.github/**`, `*.lock`, `CHANGELOG.md`), the profiles "Demo Agent (full evidence)" and
"... variant b", and runs **Refactor with Full File Evidence** and its variant: the evidence
demo agent reads three files, `src/greeter.py` and `CHANGELOG.md` through Claude-Code-shaped
`Read` tool calls whose results carry the content (the `cat -n` layout Claude Code's `Read`
tool returns) and `README.md` through a bare read report without content, searches a compact
index, runs a real, read-only `git grep` over `src/greeter.py` before changing it (a
finding cites this search, and AgentTape re-runs it automatically at the run's starting
commit), modifies `src/greeter.py` (kept), modifies and restores `README.md`, creates
`src/helpers_draft.py` and renames it to `src/formatting.py`, creates and deletes
`scratch_notes.txt`, runs a real formatter over four files (one net change), a real install
writing 300 files under `node_modules/` (collapsed), and a test command that writes
`reports/junit.xml` (generated), emits its context records, then findings F1 to F8.
F2 to F7 carry citations designed so the recorded evidence exercises every verdict the
verification engine computes (`DESIGNED_VERDICTS` in `demo_agent.py`; the agent never
records a verdict itself); F1 and F8 have no citation and are shown as declared only
(`DECLARED_ONLY_FINDINGS`). The seed adds exactly one labelled stand-in on top: F7's
own engine-computed verdict is `never_read` (the demo's evidence window is complete), so the
seed appends a `method="seeded"` `read_not_observed` row naming that engine verdict, the
closest honest analogue of a genuine incomplete evidence window until an adapter that can
produce one is part of this scenario (see `docs/CONTEXT-EVIDENCE.md` "Citation
verification"). The run asks to write `.github/workflows/ci.yml`, is approved by the seed,
resumes, and is evaluated (exit code, diff size, duration). The three commands are real `python -c`
subprocesses; their telemetry reports that argv (interpreter name, the inline script elided
as `<demo formatter script>` and so on) plus a `label`.
`tests/integration/test_evidence_demo_agent.py` runs this scenario and asserts the ledger.

```
python -m agenttape.demo_agent --workdir DIR --telemetry PATH_OR_DASH --phase start|resume \
    [--variant a|b] [--step-delay-ms N]
```

Exit 0 done, 3 needs approval (the last record before `exit` is `approval_request`), 1
failed; the final JSON line is always `{"type": "exit", "code": N}`.

## Known limitations

* Reads are only as complete as the agent's telemetry; the watcher never sees reads.
* Watcher counts for very fast bursts at the very end of an attempt are best effort (the
  watcher gets a short grace period before it stops); Git reconciliation still proves
  every net change.
* Files ignored by Git at the start have an unknown starting state (only the ignored
  entries are listed, not recursed), so their net status is `unknown`.
* Command attribution for watcher events needs a declared start/finish window (the
  evidence demo agent declares it; process events from other adapters do not).
* Group-by in the Files tab groups the current page.
* A file created and renamed inside one debounced watcher batch (300 ms) was never hashed,
  so the watcher records it as created and deleted plus a new file, not a rename; agent
  telemetry (`file_rename`) or Git reconciliation of tracked files still links the paths.
* A rebuild or reconcile (`POST /runs/{id}/files/rebuild`) is refused until the run has
  finished; while it is queued, running, or waiting for approval the worker owns its
  evidence.
* Workflow `agent_run` nodes are child runs executed by the same worker path, so they get
  full file evidence; `command` and other inline workflow nodes do not.
* If the worker dies mid-attempt, that attempt's observation window is marked
  unfinished (evidence *partial*); the next attempt's reconciliation covers the net
  state. When no attempt follows, `POST /runs/{id}/files/rebuild?reconcile=true`
  reconciles against the worktree while it still exists.
* A detected protected-path write is a review-required finding, not a pending approval
  request: only a declared intent (the agent asking first) goes through approvals.
