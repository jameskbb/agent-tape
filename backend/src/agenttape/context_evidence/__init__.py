"""Context Evidence: context packs, provenance, and the reviewed learning loop (M2).

* :mod:`.pack`      - assembles the immutable, versioned Context Pack at attempt start and
  returns the approved-lessons block AgentTape appends to the prompt.
* :mod:`.normalize` - turns persisted ``events`` rows (``context``, ``finding``,
  ``file_read``, ``tool_call``/``tool_result``) into context evidence rows, idempotently.
* :mod:`.service`   - read models (summary, provenance explorer, source detail, findings,
  comparison, project activity) and the feedback review flow.
* :mod:`.hooks`     - the worker attempt monitor (on the shared evidence feed) and terminal hook.
* :mod:`.export`    - ``context_export(session, run_id)`` for the sanitized evidence bundle.

Stage vocabulary is deliberately narrow; see docs/CONTEXT-EVIDENCE.md. Nothing here ever
claims a model "used" or "attended to" an item: AgentTape records what was available,
selected, retrieved, injected (placed verbatim into the agent's input by AgentTape),
referenced, and cited.
"""
