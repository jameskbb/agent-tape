# Context Evidence, Context Packs, and Provenance

AgentTape makes a run's *observable* context path inspectable: what instructions and
configuration were active, what AgentTape put in front of the agent, what the agent reported
searching, reading, or querying, and which sources its structured output cites. It also runs a
reviewed learning loop so that corrections improve later runs only after a person approves them.

This page defines the terms, explains how the evidence is captured, and states exactly what
AgentTape can and cannot prove.

## What AgentTape can never see

AgentTape does not see hidden model reasoning, provider-internal system prompts, or which
supplied items a model attended to. It never records "thinking" blocks, and nothing in the
product says a model "used", "reasoned from", or "paid attention to" a source. The words it
uses are: **supplied to the agent**, **retrieved by the agent**, and **cited in the output**.

## Stages

Every context record has exactly one stage. The stages are deliberately distinct.

| Stage | Meaning | Who can produce it |
|---|---|---|
| `available` | Accessible to the run but not placed in the pack (installed skills, MCP servers declared in `.mcp.json`) | AgentTape, agent telemetry |
| `selected` | AgentTape placed it in the context pack as a hashed reference. Not a claim that the model read it | AgentTape, agent telemetry |
| `retrieved` | The agent searched, opened, read, or queried it, as reported by the adapter's tool stream or the agent's telemetry | adapter events, agent telemetry |
| `injected` | AgentTape itself placed the text into the input it handed to the agent (the rendered prompt and approved lessons) | AgentTape only, or an adapter that proves it |
| `referenced` | Mentioned or linked in the agent's structured output | agent telemetry |
| `cited` | A finding, recommendation, or decision points to it as evidence | agent telemetry (`finding` links) |
| `unavailable` | The run tried to use it but access or capability was missing (MCP errors and denials land here) | adapter events, agent telemetry |
| `unknown` | The adapter cannot tell; also any unrecognised stage (never upgraded to a stronger claim) | normalizer |
| `excluded` | Considered but left out, with a reason | AgentTape, agent telemetry |
| `conflict`, `stale`, `gap` | Sources disagree, are known out of date, or needed context was missing | agent telemetry, `missing` finding links |

Every record also carries its **evidence quality**: `assembled` (AgentTape built the pack),
`adapter-observed` (seen in the adapter's own tool or file stream), `agent-declared` (the
agent's structured telemetry said so), or `user-reported`.

## The Context Pack

Before each attempt starts, the agent executor assembles one Context Pack (policy
`agenttape.default` v1). The pack holds references and provenance, never uncontrolled copies.
Items, in this fixed order:

1. **Trigger**: source (UI, API, CLI, schedule, webhook, replay, fork, workflow), reference,
   trigger or schedule id, idempotency key, and the names of the inputs it supplied.
2. **Prompt**: template and immutable version (or inline), the template hash, and the hash of
   the redacted rendered prompt. `injected` when the agent is handed it as its input;
   `selected`, with the reason, when it is not: a generic command with `prompt_mode: none`,
   and the evidence demo agent (demo `full_evidence` scenario, or a Herdr `demo` pane), which
   runs a scripted task and is never given the prompt.
3. **Variables**: values with secret variables masked and credential-shaped text redacted.
4. **Agent profile**: adapter, model, permission mode, tool allow/deny lists, turn and time
   limits, budget, sandbox settings.
5. **Instruction files** present in the working directory (`CLAUDE.md`, `CLAUDE.local.md`,
   `.claude/CLAUDE.md`, `AGENTS.md`, `GEMINI.md`, `.github/copilot-instructions.md`,
   `.cursorrules`): hashed, no excerpt stored.
6. **GSD planning references** (`.planning/PROJECT.md`, `ROADMAP.md`, `STATE.md`,
   `REQUIREMENTS.md`): hashed.
7. **Lineage**: parent, replay, fork, or workflow-node run, and inputs passed from earlier
   workflow nodes.
8. **Approved lessons** for the project (see the learning loop below): the only content
   AgentTape adds to the prompt, and the only pack items it stores an excerpt for.
9. **Compact repository index** (`git ls-files`: file count, listing hash, HEAD), recorded
   *before* any targeted file.
10. **Targeted repository files**: paths mentioned in the prompt that exist in the working
    directory, as hashed references.

Items considered but left out are recorded with a reason (a path mentioned in the prompt that
does not exist or lies outside the working directory, no Git working tree, a lesson that could
not be delivered because the agent is not given the prompt).

Each pack records its run, attempt, workflow node, version (1, 2, ... per run), policy and
policy version, creation time, ordered items, exclusions, redaction counts, the exact text
appended to the prompt, and a SHA-256 over the canonical JSON of its content. Timestamps and
per-run identifiers (trigger, schedule, idempotency key, who started the run, feedback ids)
are left out of the hash, so two runs handed the same context share it, whatever started
them. **Token estimate** is
`unavailable`: AgentTape has no tokenizer and will not estimate. A pack is written once per
attempt and never edited; an attempt resumed after an approval reuses its pack.

Workflow agent nodes run as child agent runs through the same executor, so each node gets its
own pack with the workflow inputs item.

## Capture

* **Pack assembly** runs in the agent executor before the adapter starts, because approved
  lessons must be in the prompt the adapter receives
  (`context_evidence/pack.py: prepare_attempt_context`). A failure is logged and never fails
  the run; then no pack exists and nothing is claimed.
* **Normalization** (`context_evidence/normalize.py`) turns persisted events into context
  evidence. The attempt's shared evidence feed (`worker/evidence_feed.py`, also read by File
  Evidence) hands it new events about once a second, so evidence is stored continuously; a
  run-terminal hook runs a full, idempotent re-scan. Every record is keyed on its source
  event id, so retries, restarts, and re-scans never duplicate anything. Each event is
  normalized in its own savepoint: one that cannot be stored becomes an `unknown` record
  naming the error and never blocks the events after it.

| Event | Becomes |
|---|---|
| `context` (agent telemetry) | a record at the reported stage with source, query, sanitized tool arguments, result metadata, reason; `gap`/`unavailable`/`stale`/`conflict` also open a pending feedback item |
| `context` with `stage: "correction"` | a pending `correction` feedback item on the run (never an instruction) |
| `finding` | evidence links (supports / conflicts / missing, origin `agent-declared`) plus `cited` or `gap` records |
| `file_read` | `retrieved` repository file, linked to File Evidence by repo-relative path |
| `tool_result` paired with its `tool_call` | MCP tools (`mcp__server__tool` from Claude Code, `server.tool` from Codex), `Read`, `Grep`/`Glob`, `web_search`/`WebSearch`, `WebFetch` become one `retrieved` (or `unavailable`) record with sanitized arguments, start and end time, status (`ok`, `error`, `denied`), and result hash, size, row count, schema, media type, and truncation |
| `runtime` with `action: "reconnect"` | the run's evidence window is marked incomplete with the offline interval; nothing is fabricated for the gap |

A `Read` file record also carries the range it actually covers: `requested` names the
`offset`/`limit` given (or nothing, for a whole-file read), and `returned_lines` (`start`,
`end`) comes from the `cat -n` line numbers in the tool's own result, with
`returned_truncated` when the result itself was truncated. Without this, a citation to a
line outside a partial read looks identical to one inside it. A bounded set of read-only
shell commands (`cat`, `head`, `tail`, and `sed -n '<a>[,<b>|$]p'`, with no pipes,
redirects, variables, globs, or env assignments) counts as a file read too: each command
recognized this way becomes its own `retrieved` record (source `file:<path>`) with
`shell_read` details (`program`, and whichever of `line_start`/`line_end`/`from_end`/
`whole_file`/`numbered` apply) alongside `returned_lines` when the read command names
exactly one file. Anything outside that bounded grammar (a pipeline, a redirect, an
unknown option, a wrapped invocation) stays unrecorded, exactly as before.

Sources are deduplicated per project (one `context_sources` row per stable key) while every
access stays a separate record. A repository file is always the source `file:<path>`, whether
it came from the pack, an adapter-observed read, or agent telemetry that named it with its own
id (the agent's id is kept on the record, and findings that cite it resolve to the same
source). Snapshot sources keep their synchronization time and version;
live sources are labelled live at query time. Summaries (run summary, explorer facets,
comparison, project activity) are computed from the append-only rows on every read, so the
same rows always produce the same result.

After a run completes its context evidence is not edited. Corrections are separate feedback
records linked to the run.

## Findings and evidence links

For what each verdict means to a user and what it does not show, see
[FINDINGS.md](FINDINGS.md).

A finding, claim, recommendation, decision, evaluation, generated artifact, file change, or
draft can link to sources with a relation (`supports`, `conflicts`, `missing`) and an origin
(`agent-declared`, `tool-derived`, `deterministic`, `manually-confirmed`). The Context tab
shows a findings card: one row per finding with its kind, its worst citation verdict, the
claim, and its Supports / Conflicts / Missing source chips, each row linking to that finding
on the run's Findings tab, where its cited lines are shown. The source drawer lists every
finding that links to a source.

A finding's citation-shaped links (a file line range and quote, or a reference to a recorded
event) become citations stored by `FindingsService`, the one reader and writer of findings,
citations, and citation verdicts (`docs/EVENT-SCHEMA.md` "Finding citations"). Citation
verdicts are append-only rows: a re-check adds a new row pointing at the verdict it
supersedes rather than overwriting it, and every row names the method that produced it.

**Confirming a finding.** A person can confirm a finding on a finished run
(`POST /findings/{finding_id}/confirm`, optional note) and withdraw that confirmation
(`POST /findings/{finding_id}/confirm/withdraw`). Both append an audit entry
(`finding.confirm`, `finding.withdraw_confirmation`), and a finding's `confirmation` is
derived from the latest of those entries, never stored on the finding. A confirmation never
changes a citation verdict. Three cases answer 409: confirming on a run that has not
finished (`run_not_finished`), confirming a finding that is already confirmed (`conflict`),
and withdrawing a confirmation that is not there (`conflict`).

**The Findings inbox.** `GET /findings` (the `/findings` page) lists findings across every
run recorded in a window of 1 to 90 days (default 14). By default flagged findings come
first: rows are ordered by their worst citation verdict (`quote_not_found`, `never_read`,
`stale`, `not_checkable`, `read_not_observed`, not yet checked, declared only,
`quote_matches`), newest first within a verdict; `sort=newest` orders by recorded time only.
It filters by project, agent profile, verdict, kind, launched or observed runs, and text.
The Overview's findings tile reads the same model, so the two always agree.

**Challenging findings.** `POST /runs/{run_id}/challenge` and `agenttape runs challenge`
start a reviewer child run against a finished run's findings; a `finding_review` workflow
node does the same against an upstream `agent_run` node. The reviewer sees the target's
findings and citations only inside one nonce-delimited, escaped-JSON block (never as
instructions: `docs/EVENT-SCHEMA.md` "Review findings", `docs/SECURITY.md` T27) and
records its own `upheld`/`refuted`/`inconclusive` verdict per finding, labelled
`"model judgment"`: never counted as proof, and never changing the target's own citation
verdicts. `GET /runs/{run_id}/challenges` (and `agenttape runs challenges`) lists every
challenge of a run with its outcomes per target finding; the reviewer's own citations are
verified by the same automatic verification engine described below, like any other run's.

### Citation verification

Verification runs automatically once a run reaches a terminal status (the worker's
run-terminal hook, after citations are ingested) and on an explicit
`POST /runs/{run_id}/findings/verify` re-check; never live, and never on a page read
(`GET /runs/{id}/context/findings` only renders stored verdicts). A citation is checked
against exactly what this run recorded: one documented normalization pass (line-ending and
Unicode NFKC normalization, trailing-whitespace stripping) is applied identically to the
stored content and the citation's quote before an **exact** substring match. Tabs and
quote-style characters (curly vs. straight) are never folded, and negation or word order is
never touched, so `if not authenticated:` never matches a quote reading `if authenticated:`.

Every citation gets exactly one of six verdicts:

| Verdict | What it checked |
|---|---|
| `quote_matches` | The (normalized) quote is an exact substring of what this run actually read or recorded. The true line range comes from where it matched in that recorded content; the agent's own claimed line numbers are only a hint, shown alongside it for comparison |
| `stale` | The quote matched a read, but File Evidence's own write history (never a live filesystem diff) shows the file changed later in the run; the verdict shows the change event and, when they can be established honestly, the content hash at the time of the read and the hash after the write |
| `never_read` | No read of the cited file (or the cited event) exists in this run's recording, and the run's evidence window is complete: the agent's telemetry or hooks reported no read of it. That is what it proves, not that the agent never saw the file: Claude Code loads `CLAUDE.md`, the files it imports, and files the user @-mentions into the conversation without a `Read` tool call, so no telemetry or hook reports them, and a citation of one of them is `never_read` even on a complete window (SECURITY.md, residual risks). Complete means AgentTape recorded every read that was reported: read telemetry is available, AgentTape recorded no offline gap, and, for a Claude Code session read only through its hooks (an observed session, or one hosted in a Herdr run), its hooks covered every session the run recorded from its very start (the first hook AgentTape recorded for each was `SessionStart` with source `startup`, or `clear`, since after `/clear` Claude Code starts a new session with an empty conversation; a Herdr pane can run several sessions in one run) with no dropped hook event and no AgentTape restart while they were observed, between two of them included |
| `read_not_observed` (read not observed) | Same as `never_read`, except the evidence window is *incomplete*: read telemetry was unavailable for this adapter, AgentTape recorded an offline window (a runtime reconnect), or the hooks of an observed or Herdr-hosted Claude Code session cannot show AgentTape saw all of its reads (no hook ever arrived because an MCP finding created the run; the first hook was not a fresh `SessionStart`, such as hooks installed mid-session or a resumed session; hook events were dropped; or AgentTape restarted mid-session, when Claude Code's hooks fail silently). The verdict's evidence names each gap (`window.observed.gaps`). A gap anywhere in the session counts, even one recorded after every finding: AgentTape cannot always place a lost hook in time (a hook abandoned before its body was read, a drop no session could be named for, the hooks sent while AgentTape was down), so it never assumes a gap came after the finding it would otherwise excuse. AgentTape cannot tell whether the file was read during the gap, so this is neutral, not a claim that it wasn't |
| `quote_not_found` | The file (or event) was read, and the quote is not present in what was actually returned |
| `not_checkable` | The citation could not be checked, for a stated closed-vocabulary reason: `outside_boundary`, `no_quote`, `quote_truncated`, `no_content_recorded`, `content_not_line_numbered`, `size_cap_exceeded`, `redacted_at_cited_span`, or `blob_unavailable`; never a bare "not checkable" with no reason |

The content a verdict is decided from is retained once per read, content-addressed: an
unredacted read that starts at line 1 with no gaps, was not cut by the size cap, and was
not cut by its own `limit` (a call that asked for N lines and got N back may have stopped
before the end of the file), and whose exact lines are already a blob in the run's own Git
repository, is kept as a zero-copy Git blob reference; every other read is redacted and
stored as a local copy capped at 1 MiB (cut at a line boundary). A read's output is kept
up to that 1 MiB even though the event recording it stops at the 16 KiB event text cap
(EVENT-SCHEMA.md, "A read's output past the text cap"). Those checks are what the
recording can show, not proof that a read was the whole file (an adapter's own default line
cap is not recorded), so a `stale` verdict's hash at the time of the read is only said to
be the file's own hash when it matches a hash File Evidence recorded for that file.
Retained content dies with its run, with no separate retention code path. Every verdict's
evidence carries an `inputs_digest` (a clock-free hash of the exact recorded inputs it was
decided from): an automatic pass records nothing for a citation whose digest has not
changed since its last `lexical_v1` check, so re-verifying a finished run is cheap and its
history never grows on unchanged evidence; an explicit re-check always appends, and a real
change in the recorded evidence always gets a fresh row. Each row also records the
`verifier_version` that decided it, which is part of the digest too. Version `2` added the
hook-coverage rule for observed and Herdr-hosted Claude Code sessions (a missing read there
is `never_read` only when the session's hooks covered it whole); version `1` had no such
check. Verification runs automatically only when a run finishes, so `agenttape serve`
re-checks once at start every finished observed run, and every Herdr run that hosted a
Claude Code session, whose latest `lexical_v1` verdict on some citation is a `never_read`
from an older version, recording that run's verdicts afresh (launched runs are not
re-checked: the rule does not change their verdicts). Any other run's citations can be
re-checked at any time with Re-check citations on its Findings tab or
`POST /runs/{run_id}/findings/verify`. Every row's `method` is
`lexical_v1` (the real engine) or `seeded` (the one labelled stand-in the demo seed appends
on top of F7's own engine-computed `never_read`, because the demo's evidence window is
complete and no adapter it can run produces a genuine incomplete one); a `seeded` row's
reason always starts with "Seeded demo stand-in" and its evidence names the engine verdict
and row id it stands in front of.

`tests/unit/test_findings_normalize.py` and `tests/unit/test_findings_verify.py` are the
fixture suites that pin every verdict rule the screens show.

### Reproducing cited commands

A verdict says a quote does or does not appear in what a run recorded. A reproduction goes
one step further, for the citations a lexical match cannot fully settle: it re-runs the
exact argv the run recorded and reports whether running it again, right now, at the same
commit, gets the same answer. A reproduction is stored on its own `citation_reproductions`
row and never changes the citation's verdict. The two are deliberately kept apart, since a
verdict is about what the quote said and a reproduction is about whether the command still
says it.

**When a reproduction happens.** Once a run finishes, AgentTape automatically queues one
re-run for every citation of a read-only search (`grep`, `egrep`, `fgrep`, `rg`, or
`git grep`) that has no reproduction yet, of any trigger; no one has to ask. A citation of
any other recorded command (a test suite, a build, a linter) is only re-run when a person
requests it, with `POST /citations/{citation_id}/reproductions`. Either way the request only
records a queued row; the actual re-run always happens later, in its own worker job
(`citation_reproduction`), never inside the request that queued it. At most one pending
(queued or running) reproduction per citation is allowed at a time. The database enforces
it, so two requests arriving together still queue only one, and the other gets the same
conflict a later request would; an automatic re-run is never requested twice for the same
citation.

**What can be replayed.** Only the argv AgentTape itself recorded is ever replayed: never
the finding's own prose about what ran, and never through a shell. Two shapes back a
reproduction: a recorded `command` event (a generic-command adapter's own argv, cwd, exit
code, and output), and a Claude Code `Bash` or Codex `shell` tool call, whose command is
recorded only as a single string. That string is tokenized with Python's `shlex` (never
handed to a shell), and only after it is confirmed to contain none of the characters a shell
would treat specially (a pipe, `&&`/`;` chaining, redirection, a subshell or command
substitution, a backtick, a backslash, a glob, brace or bracket expansion, `~`, `!`, or `#`),
no leading `NAME=value` assignment, no unbalanced quote, and no more than 2000 characters.
Any of those refuses the re-run outright, naming the offending character or reason, before
anything is tokenized or run. A tool call's working directory is never recorded either; a
replay of one always runs at the repository root, and its result notes that the working
directory was assumed.

Even an argv that tokenizes cleanly can still be refused. A git command is replayed only for
a subcommand that is read-only whatever its flags (`status`, `diff`, `log`, `show`,
`rev-parse`, `ls-files`, `cat-file`, `blame`, `describe`, `shortlog`, `grep`); any other
git subcommand is refused with the named reason `git_subcommand_not_replayable`, a git
global option before the subcommand (`-C <dir>`, `--git-dir`, `--work-tree`, `-c`,
`--exec-path`...) with `git_global_option_not_replayable`, and a `git-<sub>` helper
program or git run through another program (`env git ...`, `npm exec git ...`) with
`git_indirect_not_replayable`, kept on the result's evidence as `refusal`.
The snapshot shares the real repository's branches, worktree list, and config, so a replayed
`git branch <name>` or `git worktree add` would change it; neither is ever run. An argv is
also refused for a redacted value (AgentTape cannot compare something it never stored), a
path outside the repository, or an option that can make `rg` or `git grep` run another
program of its own (`rg --pre`, `rg -z`, `git grep -O`, `git grep --textconv`, and
similar). The same command policy every other
command replay goes through then decides: an approval-required or denied command is never
run (a re-run never asks for approval); only a command the policy allows outright reaches a
snapshot.

**Isolation.** An allowed re-run executes inside a detached snapshot worktree of the run's
own recorded base commit (no branch, so it never appears in `git branch`), removed again as
soon as the re-run finishes, whether it succeeded, failed, or timed out. A search is bounded
to 120 seconds; any other command inherits the project's own command timeout, capped at 10
minutes. A recorded result that reports an error but carries no exit code is refused for a
search comparison rather than silently compared: the tool's own output format in that case
is unknown, so the honest outcome is "could not run", never a wrong "not reproduced".

**Results.** Every reproduction finishes as exactly one of three results, stored with a
one-sentence, honest reason:

| Result | What it means |
|---|---|
| `reproduced` | The re-run's exit code (or failure class) matches what was recorded, and (for a search) the same output lines came back, in any order |
| `not_reproduced` | The re-run finished, but its exit code or output differs from what was recorded |
| `could_not_run` | Nothing could be compared honestly: shell syntax, a git subcommand that is not read-only, a redacted value, a path outside the repository, a program-running search option, a policy refusal, a missing Git commit to pin to, a timeout, or an error report with no exit code |

A search comparison normalizes and sorts both sides' non-blank output lines before comparing
them (through the same one documented normalization pass verification uses), so a
filesystem's own directory-listing order never causes a false `not_reproduced`; a command
comparison looks only at exit status, since a build or test command's own stdout is not a
stable thing to diff line-for-line.

**Audit.** A person's own re-run request is written to the audit log
(`citation.reproduce`, with the reproduction id, mode, and a capped argv); an automatic
re-run is not: it is a system action, and the row's own `trigger` field already says so.

## The reviewed learning loop

1. A run records a gap, an unavailable or stale source, a conflict, or a correction (from agent
   telemetry, or typed by a person in the Context tab). It is stored as **pending** feedback on
   that run.
2. A person **classifies** it: run-specific, source correction, durable business knowledge, or
   agent behavior guidance. A category suggested by the reporter is shown but never applied.
3. AgentTape **proposes** a destination: run-specific -> no promotion; source correction ->
   knowledge source update; business knowledge -> project instruction; agent behavior ->
   prompt revision. The person can change the destination and edit the lesson text.
4. A person **approves** or **rejects**. Every classify, propose, approve, and reject is written
   to the audit log.
5. Approved **project instruction** and **prompt revision** lessons get a per-project version
   (v1, v2, ...) and are appended to the prompt of every later run in that project whose
   agent is given the prompt, inside a clearly delimited block. The pack records each as
   `injected` with its version and originating run; where the prompt is not delivered the
   lesson is recorded as excluded with that reason. Knowledge source updates and
   context-provider rules are tracked decisions applied outside AgentTape and are never
   injected.

Nothing is promoted automatically, and approved lessons never rewrite prompt templates or
source-of-truth knowledge.

## Context comparison and Context Activity

The run comparison page has a Context section: sources only in A or B, stage differences,
prompt and instruction-file changes (by hash), source version and freshness changes, different
queries, MCP availability, gaps resolved or introduced, citation changes, and changed
conclusions.

The **Context** page (Context Activity, per project) shows most retrieved and most cited
sources, sources available but never selected or retrieved, stale or unavailable sources,
feedback by category, runs with missing context that failed or were corrected, evaluation
outcomes by prompt version, and approved lessons with their originating run and the number of
later runs they were injected into. Every entry links back to runs. These are associations,
not causes.

## Privacy and retention

* Secret values are never persisted: event payloads are redacted by the event sink, and the
  context modules redact again (credential patterns and sensitive keys such as `password`,
  `token`, `api_key`) and clip long arguments.
* Tool results are stored as hash, byte size, row count, schema, media type, and truncation
  flag only. Result rows, document text, and transcripts are not stored.
* Locators are display-safe: URL credentials and query strings are stripped, and absolute paths
  outside the run's working directory become `<outside>/name`.
* The only excerpts stored are approved lesson texts (human-reviewed) and the redacted original
  request, which is already stored on the run.
* Sources default to `references-and-hashes` retention. Context packs, evidence records,
  links, and feedback belong to their run and are deleted with it (retention cleanup and run
  deletion cascade). The shared per-project source registry (name, type, safe locator,
  version) is kept so later runs deduplicate against it.
* `context_export(session, run_id)`, embedded in the run evidence bundle, contains only these
  sanitized records.

## What AgentTape can and cannot prove, per adapter

| Adapter | Can prove | Cannot prove |
|---|---|---|
| Demo Agent (built-in) | The prompt and approved lessons it received; its reported file reads; its tool calls | Anything about a model (it has none; usage is simulated and labelled) |
| Evidence demo agent (demo `full_evidence`, or a Herdr `demo` pane) | Its structured `context` and `finding` telemetry, recorded as agent-declared, and its reported file reads. It is not given the prompt, so the prompt is `selected` and lessons are excluded | That a declared retrieval happened beyond the agent's own report |
| Claude Code | The prompt and approved lessons on stdin; `Read`, `Grep`, `Glob`, `WebSearch`, `WebFetch`, and `mcp__*` tool calls with their results, from the stream-json output; reported token usage | Whether it loaded `CLAUDE.md` (recorded as `selected` by hash only); what is in its system prompt; per-turn model input; anything in thinking blocks (dropped) |
| Codex CLI | The prompt and approved lessons on stdin; `mcp_tool_call` items from its JSON events; reported token usage | Whether it loaded `AGENTS.md`; web searches (Codex reports no result event for them, so they stay on the timeline only); file reads done through shell commands (visible as commands, not classified as retrievals) |
| Generic command | The prompt it was handed (stdin or argument); structured events only if its parser emits them | Anything the command does not print; with `prompt_mode: none` the prompt is not delivered, so it is recorded as `selected`, and lessons are excluded |
| Herdr runtime | Whatever the agent inside the pane reports through the telemetry file; offline intervals after an AgentTape restart | Events that happened while AgentTape was offline (the window is marked incomplete; missing events are not reconstructed) |

No adapter currently exposes per-turn model input, so no record claims `injected` for anything
AgentTape did not put into the agent's input itself.

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/runs/{run_id}/context` | Summary |
| GET | `/runs/{run_id}/context/packs` | Immutable packs with ordered items and exclusions |
| GET | `/runs/{run_id}/context/evidence` | Explorer: `stage`, `source_type`, `provider`, `node_key`, `authority`, `freshness`, `quality`, `source_id`, `q`, `limit`, `offset` |
| GET | `/runs/{run_id}/context/sources/{source_id}` | Source detail |
| GET | `/runs/{run_id}/context/findings` | Finding-to-source view. Each finding carries `id`, `key`, `kind`, `text`, `source`, its `supports`/`conflicts`/`missing` sources, `declared_only`, `details` (for a review finding, `details.review`), `confirmation` (or `null`), and its `citations`; each citation carries its latest `verdict`, `checks` (how many verdicts it has), its latest `reproduction` (or `null`), and `reproductions` (how many) |
| POST | `/runs/{run_id}/findings/verify` | Re-check a finished run's citations (appends verdicts) |
| GET | `/citations/{citation_id}/content` | The retained lines a file citation was checked against (`context`, or `before`/`after`, 0-200; at most 800 lines, `clipped` when a cited line was withheld) |
| POST | `/citations/{citation_id}/reproductions` | Queue a re-run of an event citation's recorded command (202; 422 for a file citation; 409 while the run is unfinished or a re-run is pending) |
| POST | `/findings/{finding_id}/confirm` | Confirm a finding (optional note) |
| POST | `/findings/{finding_id}/confirm/withdraw` | Withdraw a confirmation |
| GET | `/findings` | The cross-run Findings inbox: `days` (1-90, default 14), `sort` (`flagged` or `newest`), `project_id`, `agent_profile_id`, `verdict`, `kind`, `source`, `q` |
| POST | `/runs/{run_id}/challenge` | Challenge a finished run's findings with a reviewer profile |
| GET | `/runs/{run_id}/challenges` | A run's challenges and their outcomes (model judgment) |
| GET | `/runs/{run_id}/context/compare/{other_id}` | Context comparison |
| POST | `/runs/{run_id}/context/feedback` | Record a correction or lesson candidate (pending) |
| GET | `/projects/{project_id}/context/activity` | Context Activity |
| GET | `/context/feedback` | Feedback, filtered by `run_id`, `project_id`, `status` |
| POST | `/context/feedback/{id}/classify` | Classify (AgentTape proposes a destination) |
| POST | `/context/feedback/{id}/propose` | Change the destination or lesson text |
| POST | `/context/feedback/{id}/approve` | Approve (a person's decision) |
| POST | `/context/feedback/{id}/reject` | Reject |

## Data model

Migration `m2C_0001` adds five tables: `context_sources`, `context_packs` (items and exclusions
as JSON, since a pack is written once), `context_evidence_events` (append-only, unique on
`run_id` + `dedupe_key`), `evidence_links`, and `context_feedback` (ContextGap and
ContextFeedback in one table, with the review state).

Findings add six more. `m3F_0001` adds `findings`, `citations`, and `citation_verdicts`
(append-only; a re-check points at the verdict it supersedes). `m3V_0001` adds
`evidence_blobs` (retained read content, content-addressed per run, or a Git blob
reference), `evidence_reads` (one row per recorded read: its path, line range, and retained
blob), and `citation_reproductions` (one row per re-run, never changing a verdict).
`m3V_0002` adds the partial unique index `ux_citation_reproductions_one_pending` on
`citation_reproductions.citation_id` where `status` is `queued` or `running`, so a citation
has at most one pending re-run. Confirmations are audit entries, not a table. (`m3C_0001`
adds `observed_sessions` for Claude Code capture, see
[integrations/claude-code.md](integrations/claude-code.md); `m3_0002` merges the two
branches into one head.)
