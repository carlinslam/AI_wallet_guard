"""initial schema v6

Revision ID: 0001
Revises:
Create Date: 2026-06-12
"""

from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    CREATE TABLE IF NOT EXISTS agents (
        id          SERIAL PRIMARY KEY,
        name        TEXT NOT NULL UNIQUE,
        purpose     TEXT,
        daily_budget             NUMERIC(18,6) NOT NULL DEFAULT 10,
        per_tx_limit             NUMERIC(18,6) NOT NULL DEFAULT 1,
        per_minute_spend_limit   NUMERIC(18,6) NOT NULL DEFAULT 3,
        max_txs_per_minute       INTEGER NOT NULL DEFAULT 10,
        status      TEXT NOT NULL DEFAULT 'active',
        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """)
    op.execute("""
    CREATE TABLE IF NOT EXISTS transactions (
        id          SERIAL PRIMARY KEY,
        agent_id    INTEGER NOT NULL REFERENCES agents(id),
        merchant    TEXT NOT NULL,
        category    TEXT NOT NULL,
        amount      NUMERIC(18,6) NOT NULL,
        reason      TEXT,
        status      TEXT NOT NULL,
        risk_score  INTEGER NOT NULL,
        decision_reason TEXT NOT NULL,
        source      TEXT NOT NULL DEFAULT 'manual',
        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """)
    op.execute("""
    CREATE TABLE IF NOT EXISTS alerts (
        id          SERIAL PRIMARY KEY,
        agent_id    INTEGER NOT NULL REFERENCES agents(id),
        severity    TEXT NOT NULL,
        message     TEXT NOT NULL,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        acknowledged INTEGER NOT NULL DEFAULT 0
    )
    """)
    op.execute("""
    CREATE TABLE IF NOT EXISTS wallets (
        id          SERIAL PRIMARY KEY,
        agent_id    INTEGER NOT NULL UNIQUE REFERENCES agents(id),
        balance     NUMERIC(18,6) NOT NULL DEFAULT 0,
        total_earned NUMERIC(18,6) NOT NULL DEFAULT 0,
        total_spent  NUMERIC(18,6) NOT NULL DEFAULT 0,
        updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """)
    op.execute("""
    CREATE TABLE IF NOT EXISTS streams (
        id          SERIAL PRIMARY KEY,
        agent_id    INTEGER NOT NULL REFERENCES agents(id),
        provider    TEXT NOT NULL,
        unit_type   TEXT NOT NULL,
        unit_price  NUMERIC(18,8) NOT NULL,
        units_consumed NUMERIC(18,4) NOT NULL DEFAULT 0,
        total_cost  NUMERIC(18,8) NOT NULL DEFAULT 0,
        status      TEXT NOT NULL DEFAULT 'open',
        close_reason TEXT,
        started_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        ended_at    TIMESTAMPTZ
    )
    """)
    op.execute("""
    CREATE TABLE IF NOT EXISTS yield_positions (
        id          SERIAL PRIMARY KEY,
        agent_id    INTEGER NOT NULL REFERENCES agents(id),
        protocol    TEXT NOT NULL,
        principal   NUMERIC(18,6) NOT NULL,
        apy         NUMERIC(10,6) NOT NULL,
        accrued_interest NUMERIC(18,8) NOT NULL DEFAULT 0,
        status      TEXT NOT NULL DEFAULT 'active',
        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        last_accrued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        withdrawn_at    TIMESTAMPTZ
    )
    """)
    op.execute("""
    CREATE TABLE IF NOT EXISTS identities (
        id          SERIAL PRIMARY KEY,
        agent_id    INTEGER NOT NULL UNIQUE REFERENCES agents(id),
        did         TEXT NOT NULL UNIQUE,
        agent_secret TEXT NOT NULL,
        owner_commitment TEXT NOT NULL,
        verification_level TEXT NOT NULL DEFAULT 'basic',
        require_signature  INTEGER NOT NULL DEFAULT 0,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """)
    op.execute("""
    CREATE TABLE IF NOT EXISTS tax_labels (
        id          SERIAL PRIMARY KEY,
        text        TEXT NOT NULL,
        label       TEXT NOT NULL,
        source      TEXT NOT NULL DEFAULT 'seed',
        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """)
    # v6 NEW: API keys for caller authentication
    op.execute("""
    CREATE TABLE IF NOT EXISTS api_keys (
        id          SERIAL PRIMARY KEY,
        key_id      TEXT NOT NULL UNIQUE,        -- public identifier  (e.g. "awg_...")
        key_hash    TEXT NOT NULL,               -- sha256(raw_secret) — never store plaintext
        name        TEXT NOT NULL,               -- human label  ("prod key", "CI key")
        scopes      TEXT NOT NULL DEFAULT 'read,write',
        is_active   BOOLEAN NOT NULL DEFAULT TRUE,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        last_used_at TIMESTAMPTZ,
        expires_at  TIMESTAMPTZ
    )
    """)
    # v6 NEW: on-chain Solana USDC settlement records
    op.execute("""
    CREATE TABLE IF NOT EXISTS solana_payments (
        id              SERIAL PRIMARY KEY,
        transaction_id  INTEGER REFERENCES transactions(id),
        agent_id        INTEGER NOT NULL REFERENCES agents(id),
        from_pubkey     TEXT NOT NULL,
        to_pubkey       TEXT NOT NULL,
        amount_usdc     NUMERIC(18,6) NOT NULL,
        network         TEXT NOT NULL DEFAULT 'devnet',
        tx_signature    TEXT,                    -- Solana tx signature (base58)
        status          TEXT NOT NULL DEFAULT 'pending',  -- pending|confirmed|failed|simulated
        error_message   TEXT,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        confirmed_at    TIMESTAMPTZ
    )
    """)
    # Indexes for hot query paths
    for stmt in [
        "CREATE INDEX IF NOT EXISTS idx_txs_agent_created ON transactions(agent_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_txs_status ON transactions(status)",
        "CREATE INDEX IF NOT EXISTS idx_alerts_agent ON alerts(agent_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_solana_tx ON solana_payments(transaction_id)",
        "CREATE INDEX IF NOT EXISTS idx_apikeys_key_id ON api_keys(key_id)",
    ]:
        op.execute(stmt)


def downgrade():
    for tbl in ["solana_payments", "api_keys", "tax_labels", "identities",
                "yield_positions", "streams", "wallets", "alerts", "transactions", "agents"]:
        op.execute(f"DROP TABLE IF EXISTS {tbl} CASCADE")
