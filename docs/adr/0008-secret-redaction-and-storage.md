# ADR 0008: Secret redaction and storage policy

- Status: Accepted
- Date: 2026-09-23

## Context

Agents see API keys and tokens, and agent output often echoes them. The design rules out a
custom vault and forbids secret values in prompts, events, logs, exports, or screenshots.

## Decision

- AgentTape stores secret *references* only (`secret_references`: name, provider, locator,
  no value). Values are resolved at the execution boundary by a pluggable
  `SecretResolver` (environment variables by default).
- Stored prompts contain `[secret:NAME]` markers. The real value is substituted only by
  re-rendering the template in RESOLVE mode inside the worker; user-supplied inputs are
  never scanned for markers, so an input cannot pull a secret into a prompt.
- Every resolved value is added to that attempt's redactor. All persistence paths
  (events, raw logs, text artifacts, exports, audit details, error messages) pass through
  redaction, which removes configured values (`AGENTTAPE_REDACT_ENV`) and common
  credential patterns (private keys, AWS, GitHub, Anthropic, OpenAI, Slack, Google, JWT,
  bearer tokens, URL credentials, `password=`-style assignments) and records what kinds
  were removed.
- API and webhook tokens are stored only as SHA-256 hashes and compared in constant time.
- The repository's own secret scan (`scripts/secret_scan.py`, or gitleaks when present)
  runs in `make check`; test fixtures build credential-shaped strings by concatenation.

## Consequences

- Pattern redaction can produce false positives in logs; that is preferred to leaks.
- A secret split across two output lines by the agent might evade value matching; raw
  logs are redacted line by line, and patterns cover the common credential formats.
