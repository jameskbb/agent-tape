"""Not an executor: installs File Evidence collection into the worker.

Modules in this package are imported whenever a worker loads its executors (the startup
point EXTENDING.md names for worker hooks). Live collection is started by the agent
executor itself (``start_file_evidence``, before the adapter runs); the after-attempt hook
(order 55, after the Git capture at 50 and before budget checks at 90) reconciles against
Git and rebuilds the file ledger.
"""

from __future__ import annotations

from agenttape.file_evidence.collector import reconcile_after_attempt
from agenttape.worker import hooks

hooks.register_after_attempt(reconcile_after_attempt, order=55)
