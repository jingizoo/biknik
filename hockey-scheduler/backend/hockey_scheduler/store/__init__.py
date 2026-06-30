import os
from typing import Optional

from .memory_store import InMemoryStore
from .sql_store import SqlStore

__all__ = ["InMemoryStore", "SqlStore", "create_store"]


def create_store(url: Optional[str] = None):
    """Build the configured store.

    Resolution order: explicit ``url`` arg → ``DATABASE_URL`` env →
    in-memory (demo default). A URL selects the SQL store (Postgres in
    production, SQLite for local/dev/tests).
    """
    url = url or os.environ.get("DATABASE_URL")
    if url:
        return SqlStore(url)
    return InMemoryStore()
