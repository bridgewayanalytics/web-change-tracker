"""asyncpg connection pool for the pgvector / Postgres database.

The pool is created once at application startup and shared across all
requests.  Env vars consumed (see ``_pool_kwargs``):

    DATABASE_IP, DATABASE_NAME, DATABASE_USERNAME_CHATKIT, DATABASE_PASSWORD_CHATKIT, DATABASE_PORT
"""

from __future__ import annotations

import os
from typing import Optional

import ssl as _ssl_mod

import asyncpg

_pool: Optional[asyncpg.Pool] = None


def _pool_kwargs() -> dict:
    # "require" = encrypt the connection but skip certificate verification
    # (appropriate for the self-signed snakeoil cert on the database server).
    ssl_ctx = _ssl_mod.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = _ssl_mod.CERT_NONE

    return {
        "host": os.getenv("DATABASE_IP", "127.0.0.1"),
        "database": os.getenv("DATABASE_NAME", "database"),
        "user": os.getenv("DATABASE_USERNAME_CHATKIT", os.getenv("DATABASE_USERNAME", "chatkit_reader")),
        "password": os.getenv("DATABASE_PASSWORD_CHATKIT", os.getenv("DATABASE_PASSWORD", "")),
        "port": int(os.getenv("DATABASE_PORT", "6432")),
        "ssl": ssl_ctx,
        "min_size": 2,
        "max_size": 10,
        # Required for PgBouncer transaction mode: asyncpg's prepared statement
        # cache breaks when connections are multiplexed across clients.
        "statement_cache_size": 0,
        # Matches statement_timeout in postgresql.conf and query_timeout in pgbouncer.ini.
        "command_timeout": 30,
        "server_settings": {
            "application_name": "web_change_tracker",
        },
    }


async def init_pg_pool() -> asyncpg.Pool:
    """Create the module-level connection pool (call once at startup)."""
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(**_pool_kwargs())
    return _pool


async def close_pg_pool() -> None:
    """Gracefully close the pool (call at shutdown)."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pg_pool() -> Optional[asyncpg.Pool]:
    """Return the live pool, or ``None`` if not yet initialised."""
    return _pool
