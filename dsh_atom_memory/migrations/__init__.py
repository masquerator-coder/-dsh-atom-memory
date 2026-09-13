"""Bundled SQL migrations for dsh-atom-memory.

Each file is named ``NNN_*.sql`` and is applied in numeric order, gated by
``PRAGMA user_version`` (see :func:`dsh_atom_memory.db.open_db`).
"""
