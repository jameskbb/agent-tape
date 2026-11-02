"""Herdr runtime integration (docs/HERDR.md, ADR 0009).

Herdr owns terminals, panes, agent processes, and session persistence; AgentTape talks to
it over its local socket API to start, prompt, observe, and reattach, and records what it
did as ``runtime`` events and a ``runtime_sessions`` row per attempt.
"""
