# ADR 0009: Herdr owns the agent runtime; AgentTape owns control and evidence

- Status: Accepted
- Date: 2026-09-23

## Context

AgentTape runs agents as child processes of its worker. That path cannot keep an
interactive agent alive across an AgentTape restart, cannot let a person look at or answer
a waiting agent, and knows nothing about Claude Code's or Codex's native sessions. Adding
those things would mean building a terminal multiplexer: panes, a PTY server, session
persistence, remote machines, and agent-state detection.

[Herdr](https://herdr.dev) already does that job. It keeps terminals, panes, workspaces,
and agent processes alive in a server, recognises agents and classifies them as idle,
working, blocked, done, or unknown, reaches remote machines over SSH, and restores native
sessions. It exposes all of that through a local Unix-socket API (newline-delimited JSON,
protocol 22 in Herdr 0.9.0) and a CLI.

## Decision

- **Herdr is where agents live.** AgentTape does not grow terminal, PTY, remote-machine, or
  session-restoration code. The `herdr` adapter drives Herdr through its socket API:
  `workspace.create`/`tab.create`, `agent.start`, `agent.prompt` (never raw keystrokes for a
  prompt), `events.subscribe` with bounded `agent.get` polling as the fallback, `agent.read`,
  and `agent.send_keys ctrl+c` for cancellation.
- **AgentTape records why agents ran, governs what they may do, and proves what they
  changed.** Runs, attempts, approvals, evaluations, budgets, file and context evidence, and
  audit history stay in AgentTape and work the same for every runtime.
- **Runtimes declare capabilities.** Each runtime/agent combination states which of start,
  prompt, observe_state, subscribe_events, read_output, interrupt, reconnect, attach, and
  file_telemetry it supports, supports in a degraded form, or does not support
  (`herdr/capabilities.py`). The UI shows the table; nothing pretends.
- **Identity is persisted per attempt** in `runtime_sessions` as soon as the pane exists:
  machine and session labels, Herdr version and protocol, workspace/tab/pane ids, agent
  name and kind, whether a native session reference exists (never its value), lifecycle
  state, ownership flags, telemetry offset, and last sync. Socket paths are never stored or
  shown.
- **Restarts reattach instead of failing.** When a worker stops or its lease expires, an
  attempt whose Herdr session is recorded active is parked and re-queued; the next worker
  reopens the same attempt, queries Herdr, emits a `runtime` `reconnect` event with the
  offline window, and continues. A dead, replaced, or missing session fails with a
  structured category (`stale_id`, `agent_replaced`, `unavailable`, ...).
- **Ownership is explicit and conservative.** AgentTape only ever closes a workspace or
  tab it created and recorded as owned, and only when the profile asks for
  `cleanup: close_owned`. A blocked agent is never answered or interrupted by AgentTape.
- **Fallback is visible and pre-start only.** Local and demo execution remain first-class
  and need no Herdr. A Herdr profile may fall back to the equivalent local adapter only
  before the attempt starts, and the fallback is recorded. Nothing falls back after a
  Herdr start.
- **Remote machines stay in Herdr.** AgentTape talks to one configured local socket and
  lists saved machines read-only from `herdr machine list --json`; it contains no SSH code.

## Consequences

- Claude Code and Codex under Herdr give AgentTape lifecycle state but no tool-level
  telemetry, token usage, or cost; those show as unavailable, and File Evidence relies on
  filesystem observation and Git reconciliation for them.
- A person can attach to a live run (`herdr agent attach <name>`), which the local
  process path could never offer.
- The adapter depends on Herdr's protocol. Older protocols are refused with a clear
  message; newer ones run with a recorded warning. The fake server in
  `backend/tests/fixtures/fake_herdr.py` keeps tests and CI independent of a real Herdr;
  responses captured from a real Herdr (`backend/tests/fixtures/herdr_protocol.json`) keep
  the fake and the client honest about the real shapes.
