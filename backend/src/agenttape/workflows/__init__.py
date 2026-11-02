"""The workflow orchestration engine.

* :mod:`agenttape.workflows.engine` - the durable DAG scheduler run by the ``workflow``
  run executor (``worker/executors/workflow.py``).
* :mod:`agenttape.workflows.nodes` - one module per node type, auto-discovered.
* :mod:`agenttape.workflows.maintenance` - the sweep that re-wakes workflow runs whose
  wake-up was lost (crash, restart) and expires overdue approvals.

See docs/ORCHESTRATION.md.
"""
