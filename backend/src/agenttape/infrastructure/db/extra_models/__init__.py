"""Additional ORM tables, added after the core model.

Add a module here (e.g. ``schedules_runtime.py``) that defines models on
``agenttape.infrastructure.db.base.Base``; it is imported automatically at the end of
``models.py`` so Alembic autogenerate, the app, and tests all see it. Prefer the JSON
columns that core tables already have (``settings``, ``config``, ``options``, ``budget``,
``metadata``) before adding columns to core tables.
"""
