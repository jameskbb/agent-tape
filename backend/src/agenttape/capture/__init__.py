"""Pure mapping of Claude Code capture payloads (hooks, and later MCP) into AgentTape's
event vocabulary. Nothing in this package touches the database, the filesystem, or the
network - it only parses already-received JSON and builds :class:`EventDraft` objects for
callers (``application/services/observed_sessions.py``) to persist through the existing
``EventRecorder``.
"""

from __future__ import annotations
