# ADR 0007: Prompt, profile, and workflow versioning

- Status: Accepted
- Date: 2026-09-23

## Context

Reproducibility requires knowing exactly which prompt, agent configuration, workflow,
inputs, and repository state produced a run, even after those are edited.

## Decision

- Prompt templates, agent profiles, workflows, and evaluator definitions are a mutable
  header row plus immutable numbered version rows (`*_versions`, unique per parent and
  version). Every edit creates a new version with a content hash; nothing edits a version
  in place. A version gets `locked_at` the first time a run uses it.
- Run creation writes an immutable JSON snapshot on the run: prompt body, variables and
  rendered text (secret markers only), agent profile config, workflow definition, inputs,
  project settings, Git state, environment fingerprint, and run options, plus a snapshot
  hash. Foreign keys from runs to configuration are `SET NULL` on delete because the
  snapshot is the record of truth.
- Prompt templates use a deliberately tiny syntax: `{{ name }}` for typed variables and
  `{{ secret:NAME }}` for secret references. No expressions, loops, or code execution.
- Replays re-use a run's snapshot; forks create a new run with selected changes and
  `parent_run_id` / `parent_relation` links.

## Consequences

- History is stable and comparable; storage grows with each version, which is small text.
- Workflow node and edge tables are also keyed by version, so a running workflow keeps its
  definition while a new version is drafted.
