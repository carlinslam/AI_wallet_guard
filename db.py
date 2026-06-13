"""
AI Wallet Guard v6 — database layer

Primary: PostgreSQL via SQLAlchemy + psycopg2 connection pool.
Fallback: SQLite (for zero-config local dev / CI).

Connection string is resolved in this priority order:
  1. DATABASE_URL environment variable  (e.g. postgres://user:pass@host:5432/db)
  2. .env file in the project root       (DATABASE_URL=...)
  3. Compiled default                    (postgres://awg_user:awg_pass@localhost:5432/ai_wallet_guard_v6)
  4. SQLite file                         (if DATABASE_BACKEND=sqlite or Postgres unreachable)

Usage (replaces sqlite3 calls in storage.py):
    from db import engine, get_conn, execute, query_df

All functions are thin wrappers that speak pure SQL so the rest of the
codebase keeps its existing query strings unchanged.
"""

import os
import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

import pandas as pd
from sqlalchemy import create_engine, text, event
from sqlalchemy.pool import QueuePool

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _load_dotenv():
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

_load_dotenv()

SQLITE_FALLBACK_PATH = Path(__file__).parent / "ai_wallet_guard_v6.db"

_POSTGRES_DEFAULT = (
    "postgresql+psycopg2://awg_user:awg_pass@localhost:5432/ai_wallet_guard_v6"
)


def _build_url() -> tuple[str, str]:
    """Return (url, backend_name)."""
    if os.environ.get("DATABASE_BACKEND", "").lower() == "sqlite":
        return f"sqlite:///{SQLITE_FALLBACK_PATH}", "sqlite"
    url = os.environ.get("DATABASE_URL", _POSTGRES_DEFAULT)
    # Heroku-style postgres:// → postgresql+psycopg2://
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg2://", 1)
    if not url.startswith("postgresql"):
        return f"sqlite:///{SQLITE_FALLBACK_PATH}", "sqlite"
    return url, "postgres"


def _make_engine(url: str, backend: str):
    if backend == "sqlite":
        eng = create_engine(
            url,
            connect_args={"check_same_thread": False},
            echo=False,
        )
        # SQLite WAL mode for better concurrency
        @event.listens_for(eng, "connect")
        def _set_wal(conn, _):
            conn.execute("PRAGMA journal_mode=WAL")
        return eng
    return create_engine(
        url,
        poolclass=QueuePool,
        pool_size=5,
        max_overflow=10,
        pool_timeout=30,
        pool_pre_ping=True,   # recycles stale connections
        echo=False,
    )


def _probe(eng) -> bool:
    try:
        with eng.connect() as c:
            c.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        log.warning("Database probe failed: %s", exc)
        return False


# Build the engine once at import time, falling back to SQLite if needed.
_url, _backend = _build_url()
engine = _make_engine(_url, _backend)

if not _probe(engine):
    log.warning("Postgres unreachable — falling back to SQLite.")
    _url, _backend = f"sqlite:///{SQLITE_FALLBACK_PATH}", "sqlite"
    engine = _make_engine(_url, _backend)

BACKEND = _backend
log.info("Database backend: %s  url: %s", BACKEND, _url.split("@")[-1])  # hide credentials

# ---------------------------------------------------------------------------
# Public helpers (drop-in replacements for the v5 sqlite3 helpers)
# ---------------------------------------------------------------------------

@contextmanager
def get_conn():
    """Yield a SQLAlchemy connection (auto-commits, auto-closes)."""
    with engine.connect() as conn:
        yield conn


def execute(query: str, params: tuple = ()) -> Optional[int]:
    """
    Execute a DML statement. Returns the inserted row id for INSERTs, or None.
    Accepts positional-? (sqlite style) automatically converted to named :pN (SQLAlchemy).
    """
    q, p = _normalise(query, params)
    is_insert = q.strip().upper().startswith("INSERT")
    with engine.begin() as conn:
        result = conn.execute(text(q), p)
        if is_insert:
            # For Postgres with RETURNING id clause, use fetchone()
            if BACKEND == "postgres":
                row = result.fetchone()
                return row[0] if row else None
            # SQLite: use lastrowid
            return result.lastrowid
        return None


def query_df(query: str, params: tuple = ()) -> pd.DataFrame:
    """Run a SELECT and return a DataFrame."""
    q, p = _normalise(query, params)
    with engine.connect() as conn:
        return pd.read_sql_query(text(q), conn, params=p)


def fetchone(query: str, params: tuple = ()) -> Optional[dict]:
    """Return the first row as a dict, or None."""
    q, p = _normalise(query, params)
    with engine.connect() as conn:
        row = conn.execute(text(q), p).mappings().fetchone()
        return dict(row) if row else None


def fetchall(query: str, params: tuple = ()) -> list[dict]:
    """Return all rows as a list of dicts."""
    q, p = _normalise(query, params)
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(q), p).mappings().fetchall()]


# ---------------------------------------------------------------------------
# Param normalisation: ? → :p0, :p1, … (SQLAlchemy named style)
# Also handles Postgres RETURNING id clause for INSERT … RETURNING id.
# ---------------------------------------------------------------------------

def _normalise(query: str, params: tuple) -> tuple[str, dict]:
    """Convert positional-? SQL + tuple params → named-:pN SQL + dict."""
    named_params = {}
    idx = [0]

    def _replace(_):
        key = f"p{idx[0]}"
        named_params[key] = params[idx[0]]
        idx[0] += 1
        return f":{key}"

    import re
    named_query = re.sub(r"\?", _replace, query)

    # For SQLite INSERT we rely on lastrowid; for Postgres we need RETURNING id.
    if (BACKEND == "postgres"
            and named_query.strip().upper().startswith("INSERT")
            and "RETURNING" not in named_query.upper()):
        named_query = named_query.rstrip().rstrip(";") + " RETURNING id"

    return named_query, named_params
