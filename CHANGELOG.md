# Changelog

All notable changes to AgentTape. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow semantic
versioning once a release is published.

## [Unreleased] - M3: Verified findings (trust but verify)

Agents cite evidence for what they find; AgentTape, not the AI, checks each citation
against its own recording of the run and shows the exact cited lines with a verdict that
says what it checked. Checkable, not foolproof: a verdict never says a finding is true, and
a well-cited finding can still be wrong. M3 was accepted on a real Claude Code analysis of
this repository, both launched by AgentTape and in an observed session, that caught a bad
citation. See [docs/FINDINGS.md](docs/FINDINGS.md) and the
[Claude Code integration](docs/integrations/claude-code.md).

The acceptance, stated plainly: the caught citation quoted lines the agent had seen only in
a Grep result and cited them as a file it read, which AgentTape showed as "Never read"; the
quote itself was accurate. The same acceptance found one wrong verdict from AgentTape (a
redaction defect, fixed below) and reads cut at the event cap (also fixed below; runs
recorded before a fix keep their verdicts). The launched analysis cost 0.46 USD as Claude
Code reported it to AgentTape; the observed session cost 0.62 USD as Claude Code's own
result JSON reported it, because hooks carry no cost and AgentTape shows it as not reported.

### Verified by hand

- The Python test suite, migrations, and a demo instance run on PostgreSQL 17 (2134 passed,
  one SQLite-only test skipped). The native install needs `psycopg[binary]` added.
- The Docker image builds and runs: API, web UI, demo and generic-command runs, and data
  surviving a restart. It is SQLite-only. `AGENTTAPE_UID` and `AGENTTAPE_GID` build
  arguments match the container user to a mounted repository's owner.

### Added

- **Findings and citations**: one `FindingsService` records every finding and its
  citations through the extended `finding` event. A citation is a file citation (path, a
  quote copied exactly, and line numbers as a hint) or an event citation (the `tool_use`
  id of a call whose output is quoted); a finding with no citation is shown as "Declared
  only".
- **Capture for launched runs**: the `record_finding` MCP tool over stdio. Each launched
  Claude Code attempt gets its own AgentTape MCP server, the tool pre-approved, and the
  findings contract appended to its system prompt.
- **Capture for interactive and Herdr sessions**: `agenttape mcp register`,
  `agenttape mcp unregister`, and `agenttape mcp status` for your own Claude Code sessions;
  Claude Code hooks installed by `agenttape hooks setup`, checked by
  `agenttape hooks status`, and removed by `agenttape hooks remove`. A session in a registered project becomes an
  observed run, or lands on the Herdr run hosting its pane, with a `session_id` fallback
  when one directory has more than one active session.
- **Read capture**: reads from Claude Code's `Read` tool (with offset and limit) and from a
  narrow set of read-only shell commands (`cat`, `head`, `tail`, `sed -n` with a line
  range).
- **Verification**: a deterministic engine with verdicts Quotes match, Stale, Never read,
  Read not observed, Quote not found, and Cannot check (each with its reason), plus
  Declared only; one documented normalization; retained, redacted, capped evidence with
  Git blob references; and an append-only re-check history. It runs when a run finishes
  and on request.
- **Re-runs**: cited read-only searches are re-run automatically and cited commands on
  request, from the recorded argument list, never through a shell, in a detached snapshot
  of the repository at the run's base commit; a re-run never changes a verdict.
- **Challenge**: Challenge findings starts a reviewer as a child run whose opinion
  (upheld, refuted, or inconclusive) is labelled model judgment and verified like any
  other citation; `agenttape runs challenge` and `agenttape runs challenges`, and the
  `finding_review` workflow node.
- **Findings UI**: the run's Findings tab with tally chips, the citation viewer and its
  evidence ladder, confirm and withdraw, the Findings inbox, the Overview's "Verified
  findings" tile, the Context tab's findings card, the run header tally, and findings in
  exports.
- **Seeded demo**: a no-model seeded demo that produces every verdict, with its one
  stand-in labelled "Seeded demo stand-in".
- **Docs and positioning**: [docs/FINDINGS.md](docs/FINDINGS.md), the Claude Code
  integration guide, and a README that leads with trust but verify, with orchestration
  as the foundation. `scripts/check.sh` fails on any em dash in a tracked file.
- Migrations `m3_0001` (merge of the M2 heads), `m3F_0001` (findings), `m3C_0001` (observed
  sessions), `m3V_0001` (verification), `m3V_0002` (one pending reproduction), and
  `m3_0002` (merge of the M3 heads).

### Fixed

- **SessionStart capture**: Claude Code never runs an `http` hook on SessionStart, so
  `agenttape hooks setup` now installs SessionStart as an exec-form command hook, a small
  relay that posts the event to the same loopback endpoint without a shell and never
  fails the session. `agenttape hooks status` shows the relay and how its last run ended.
- **Redaction of JSON output lines**: a Claude Code stream-json line is redacted
  string by string, so redaction can no longer break the line and lose a tool result.
- **Long reads**: a read's output is retained for verification up to
  1 MiB instead of being cut at the 16 KiB event cap, Claude Code stream-json lines may be
  up to 8 MiB, and redaction, retention, and the citation view's wording were hardened.
- **Findings contract**: the contract and the `record_finding` tool description say that
  text seen only in Grep or Glob output is not a file read, so the agent reads the file
  before a file citation or cites the search as an event citation.

### Changed

- Licensed under the MIT License. Contributor rules are in `CONTRIBUTING.md` and
  `docs/DEVELOPMENT.md`.

### Security

- **Capture ingest hardening**: the MCP and hook endpoints accept loopback only, verify
  hashed tokens, cap request sizes, rate-limit with `Retry-After`, and redact before
  anything is stored; hooks fail open inside 3 seconds; capture files are owner-only.
  docs/SECURITY.md T23 to T29.

## [Unreleased] - M2: Herdr runtime, file evidence, context provenance

### Added

- **Herdr runtime**: a `herdr` agent adapter that runs Claude Code, Codex, or the Demo
  Agent in a Herdr workspace and pane through Herdr's socket API (protocol 22), prompts
  agents through Herdr's agent interface, follows working/blocked/done/idle/unknown state
  over an event subscription with bounded polling as fallback, persists stable runtime
  references per attempt, reattaches after an AgentTape restart instead of failing the
  run, never answers a blocked agent, and only closes workspaces it created. Pre-start
  fallback to the local runtime is explicit and visible; there is no fallback after a run
  starts. Runtime settings page (installed, reachable, version, machines, integrations,
  capabilities, connection test) and a run Runtime tab with a copyable attach command.
  ADR 0009 and docs/HERDR.md.
- **File evidence**: append-only file evidence events from agent telemetry, live
  filesystem observation (watchfiles), and Git start/end reconciliation, rebuilt into a
  per-run file ledger with precise labels (observed read, inferred access, created,
  modified, renamed, deleted, restored, net unchanged, generated, outside boundary,
  unknown) and an evidence-completeness statement. Protected-path and outside-boundary
  findings, collapsed noisy directories, the run Files tab, the project File Activity
  page, and sanitized CSV/JSON ledger and evidence-bundle exports. docs/FILE-EVIDENCE.md.
- **Context evidence**: immutable, hashed context packs per attempt; context sources with
  authority and snapshot/live freshness; provenance events that keep available,
  selected, retrieved, injected, referenced, and cited distinct; finding-to-source
  links; gaps, conflicts, and stale sources; a reviewed learning loop in which
  corrections stay tied to their run until a person approves them as versioned lessons
  that later runs record as injected. Run Context tab, project Context page, and a
  context section in Compare. docs/CONTEXT-EVIDENCE.md.
- **Evidence demo**: `python -m agenttape.demo_agent`, a deterministic agent that
  exercises every file and context evidence case, the Demo Agent `full_evidence`
  scenario, and seeded runs "Refactor with Full File Evidence" (and a variant b for
  comparison).
- Event types `runtime`, `file_evidence`, `context`, and `finding`.
- Migrations `m2A_0001` (runtime sessions), `m2B_0001` and `m2B_0002` (file evidence), `m2C_0001`
  (context evidence), branching from `trackE_0001`.

## [Unreleased] - 0.1.0 (V1)

### Added

- **Run engine**: one execution path for runs started from the UI, CLI, API, schedules,
  webhooks, and workflows; idempotent creation; immutable configuration snapshots;
  run, attempt, and step state machines; a database-backed queue with leases; workers
  with heartbeats, wall-clock and idle timeouts, process-group cancellation, retry with
  backoff, and recovery of interrupted work.
- **Flight recorder**: a vendor-neutral, sequenced event stream with redaction and size
  caps, batched output with full raw logs, live Server-Sent Events with resume, a
  filterable timeline with a raw JSON inspector, and sanitized JSON and zip exports.
- **Adapters**: the built-in Demo Agent (no credentials), generic command (allowlisted
  executables, argument arrays), Claude Code, and Codex CLI, with discovery that never
  needs credentials.
- **Prompts, profiles, projects**: versioned prompt templates with typed variables and
  secret references, history, diff, duplicate, import and export; versioned agent
  profiles validated before saving; projects confined to allowlisted roots; onboarding.
- **Workflows**: versioned DAG workflows with agent, command, transform, evaluation gate,
  approval, condition, and collect nodes; typed outputs; parallel branches; durable
  pause and resume without re-running completed nodes; a graph and list view.
- **Approvals and safety policy**: approval inbox, expiry, audit entries, approvals
  required by default for risky actions, and actions refused even with approval.
- **Evaluations**: built-in evaluators (exit code, command, file checks, diff size and
  protected paths, regex, JSON Schema, artifacts, duration, usage and cost thresholds,
  human score), versioned definitions, suites, required gates, rerun, and comparison.
- **Scheduling and triggers**: one-time, interval, and cron schedules with IANA time
  zones and DST-correct next runs, misfire and concurrency policies, run now, history, a
  lease-owned scheduler, and authenticated, rate-limited, idempotent webhooks.
- **Budgets and limits**: token, cost, changed-file, diff-line, and artifact budgets with
  warnings before the boundary, and global, project, adapter, and workflow concurrency.
- **Git**: isolated AgentTape-owned worktrees, starting revision and dirty warnings,
  change capture and diff viewer, protected paths, validation commands, approved local
  commits, and safe worktree cleanup.
- **Replay, fork, compare**: replay with identical versions and inputs, fork with
  changes, environment drift warnings, side-by-side comparison, and lineage links.
- **Overview dashboard**: live work, waiting approvals, upcoming schedules, recent
  failures, success rate, median duration, daily outcomes, top failure categories, and
  real usage and cost only when reported.
- **Demo story**: `agenttape seed` creates a coherent demo (projects, profiles, prompts,
  a paused nightly schedule, the Safe Code Change workflow waiting on approval, completed,
  retried, failed, and timed-out runs, and a pair to compare); `reset-demo` removes only
  demo data.
- **Operations**: `agenttape` CLI (`init`, `doctor`, `serve`, `worker`, `scheduler`,
  `demo`, `run`, `runs`, `schedules`, `triggers`, `export`, and more), a Dockerfile and
  Compose example, retention with dry-run preview, and `make check` with every quality
  gate including accessibility checks.

### Security

- Loopback bind by default; hashed API and webhook tokens; argument arrays only;
  allowlisted environments and executables; redaction on every persistence path;
  guarded Git runner; path traversal protection; size caps. See docs/SECURITY.md.

### Known limitations

- PostgreSQL and the Docker image were not yet verified at this point; see the M3 section.
- No agent-as-judge evaluator backend ships; the interface reports it as unavailable.
- Workflow runs can be replayed but not forked.
