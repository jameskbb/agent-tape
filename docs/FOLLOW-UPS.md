# Known limitations and open items

Residual limitations and rough edges beyond the ones in the
[README](../README.md#known-limitations). None blocks local use. Ordered roughly by impact.

## Security and adapters

1. **An allowlisted wrapper or interpreter can still run guarded commands internally.** The V1
   command policy judges the argument array AgentTape itself launches, so it refuses `git push`,
   destructive resets, `rm`, shells, and shell-form git aliases from a process adapter. It cannot
   see inside a program that is not what it claims to be: an allowlisted shell script, `python`,
   or a custom wrapper whose own code runs `git push`. This is the same class of trust as
   allowlisting `bash`, and it is stated in [SECURITY.md](SECURITY.md) and the command-policy
   module. Allowlisting a script or interpreter grants it full command latitude, so prefer
   concrete tools. First-class sandboxing (container or VM) for untrusted agents is out of scope
   for V1.

2. **Alias resolution runs a short-lived `git config --get` per approval-gated git command.**
   The command policy runs it (read-only, argument array) to expand git aliases before it judges
   a command. It is bounded and safe, but adds a subprocess in the run's working directory. The
   result is not cached.

## Frontend

3. **Overview "Top failure categories" tiles link to the unfiltered failures list.** A click
   lands on all failed and timed-out runs, not on just that category
   (`features/overview/OverviewPage.tsx`).

4. **Segmented radio groups lack arrow-key navigation.** The theme switch, the overview range
   toggle, the agent config editor, and the schedule kind selector work with Tab and Enter but
   not arrow keys, which is the expected ARIA radiogroup interaction.

5. **Workflow editor: evaluator config is edited as raw JSON.** There is no structured form for
   the common built-in evaluators.

6. **Workflow editor loses unsaved JSON-field and key drafts when switching editor tabs**,
   because only the active tab panel is mounted. There is no warning before the draft is
   discarded.

## Tooling

7. **The built-in secret scan is a heuristic.** `make check` uses `gitleaks` when it is
   installed. Otherwise it falls back to built-in patterns, which catch unquoted assignments,
   JWTs, and bearer tokens and report opt-outs, but can miss other shapes.
