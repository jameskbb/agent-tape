# ADR 0005: CLI adapter security model

- Status: Accepted
- Date: 2026-09-23

## Context

Adapters launch local programs (Claude Code, Codex, arbitrary allowlisted commands) with
prompts and inputs that may come from webhooks or other people. Shell injection, leaked
environment variables, runaway processes, and secrets in logs are the main risks.

## Decision

- Every process starts through `agenttape.adapters.process.run_process`, which uses
  `asyncio.create_subprocess_exec` with an argument array. `shell=True` and interpolated
  command strings are never used; executables and arguments are stored separately.
- Child environments are built from a small safe base (`PATH`, `HOME`, locale, `TMPDIR`,
  user) plus a profile's explicit `env_allowlist` plus resolved secret references. The
  parent environment is not inherited wholesale.
- Each child runs in its own session/process group. Cancellation, wall-clock timeout,
  idle timeout, and lease loss send SIGTERM to the group, then SIGKILL after a grace
  period; lingering descendants holding the pipes are killed when the leader exits.
- stdout and stderr are drained concurrently in 64 KB chunks with bounded line buffers,
  redacted, written to capped raw logs, and forwarded as events.
- Working directories must be inside allowlisted, canonicalised project roots (symlinks
  resolved). The Demo Agent only writes inside its own clone under the data dir.
- Profiles are validated by their adapter before they are saved; adapter discovery never
  requires credentials and never crashes callers.

## Consequences

- POSIX only (Linux, macOS, WSL). Windows needs a job-object implementation later.
- Adapters that need interactive terminals are out of scope; structured, non-interactive
  modes are required.
