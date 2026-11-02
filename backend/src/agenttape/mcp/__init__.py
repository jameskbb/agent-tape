"""The AgentTape MCP server: a thin stdio-to-loopback-HTTP caller.

``agenttape.mcp`` never opens the database - it makes one HTTP call per tool invocation to
the loopback FastAPI app (``POST /api/v1/mcp/findings``), which writes through the same
``EventRecorder``/``FindingsService`` path every other event source uses.
"""

from __future__ import annotations
