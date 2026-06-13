"""
AI Wallet Guard v6 — shared database + risk engine + wallets + yield primitives.

v6: SQLite replaced with PostgreSQL via db.py (SQLAlchemy + psycopg2).
All raw sqlite3 calls replaced with db.execute / db.query_df / db.fetchone.
Schema is managed by Alembic migrations (run: alembic upgrade head).
"""

import os
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, List

import pandas as pd

from db import execute, query_df, fetchone, fetchall

WALLET_SEED_MULTIPLIER = 3.0  # initial wallet balance = daily_budget * 3

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def now_dt() -> datetime:
    return datetime.now(timezone.utc)



def seed_data():
    count_row = fetchone("SELECT COUNT(*) AS c FROM agents")
    count = int(count_row["c"] if count_row else 0)

    if count == 0:
        agents = [
            ("Research Agent", "Autonomous research agent that buys small paid data/API calls", 20, 1.50, 4.00, 15, "active"),
            ("Image Agent", "Autonomous image agent that pays for image generation and media processing", 30, 3.00, 8.00, 10, "active"),
            ("Crawler Agent", "Autonomous crawler that collects web data from paid APIs", 8, 0.20, 1.00, 25, "active"),
            ("Plugin Seller Agent", "Agent that SELLS a popular plugin and earns micropayment revenue", 15, 1.00, 5.00, 30, "active"),
        ]
        for a in agents:
            execute("""
            INSERT INTO agents
            (name, purpose, daily_budget, per_tx_limit, per_minute_spend_limit, max_txs_per_minute, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (*a, now_iso()))

    # ensure every agent has a wallet
    agent_rows = fetchall("SELECT id, daily_budget FROM agents")
    for row in agent_rows:
        wallet = fetchone("SELECT id FROM wallets WHERE agent_id = ?", (row["id"],))
        if not wallet:
            execute("""
                INSERT INTO wallets (agent_id, balance, total_earned, total_spent, updated_at)
                VALUES (?, ?, 0, 0, ?)
            """, (row["id"], float(row["daily_budget"]) * WALLET_SEED_MULTIPLIER, now_iso()))

def reset_demo_data():
    for table in ("solana_payments", "alerts", "transactions", "streams",
                  "yield_positions", "wallets", "identities", "tax_labels", "agents"):
        execute(f"DELETE FROM {table}", ())
    # Reset Postgres sequences
    for table in ("agents","transactions","alerts","streams","yield_positions",
                  "wallets","identities","tax_labels","solana_payments"):
        execute(f"ALTER SEQUENCE IF EXISTS {table}_id_seq RESTART WITH 1", ())
    seed_data()

# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------

def get_agent(agent_id: int) -> Optional[Dict[str, Any]]:
    return fetchone("SELECT * FROM agents WHERE id = ?", (agent_id,))

def get_agent_by_name(agent_name: str) -> Optional[Dict[str, Any]]:
    return fetchone("SELECT * FROM agents WHERE LOWER(name) = LOWER(?)", (agent_name,))

def create_agent(name, purpose, daily_budget, per_tx_limit, per_minute_spend_limit, max_txs_per_minute, status="active"):
    agent_id = execute("""
        INSERT INTO agents
        (name, purpose, daily_budget, per_tx_limit, per_minute_spend_limit, max_txs_per_minute, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (name.strip(), purpose, float(daily_budget), float(per_tx_limit),
          float(per_minute_spend_limit), int(max_txs_per_minute), status, now_iso()))
    # issue the virtual sub-wallet at creation time
    execute("""
        INSERT INTO wallets (agent_id, balance, total_earned, total_spent, updated_at)
        VALUES (?, ?, 0, 0, ?)
    """, (agent_id, float(daily_budget) * WALLET_SEED_MULTIPLIER, now_iso()))
    return agent_id

def update_agent_policy(agent_id, name, purpose, daily_budget, per_tx_limit, per_minute_spend_limit, max_txs_per_minute, status):
    execute("""
        UPDATE agents
        SET name = ?, purpose = ?, daily_budget = ?, per_tx_limit = ?, per_minute_spend_limit = ?,
            max_txs_per_minute = ?, status = ?
        WHERE id = ?
    """, (name.strip(), purpose, float(daily_budget), float(per_tx_limit),
          float(per_minute_spend_limit), int(max_txs_per_minute), status, agent_id))

# ---------------------------------------------------------------------------
# Wallets (虛擬子卡 / simulated USDC)
# ---------------------------------------------------------------------------

def get_wallet(agent_id: int) -> Optional[Dict[str, Any]]:
    return fetchone("SELECT * FROM wallets WHERE agent_id = ?", (agent_id,))

def credit_wallet(agent_id: int, amount: float):
    """Agent EARNS money (e.g. selling plugin calls)."""
    execute("""
        UPDATE wallets
        SET balance = balance + ?, total_earned = total_earned + ?, updated_at = ?
        WHERE agent_id = ?
    """, (float(amount), float(amount), now_iso(), agent_id))

def debit_wallet(agent_id: int, amount: float) -> bool:
    """Debit if funds are sufficient. Returns True on success."""
    wallet = get_wallet(agent_id)
    if not wallet or wallet["balance"] < amount:
        return False
    execute("""
        UPDATE wallets
        SET balance = balance - ?, total_spent = total_spent + ?, updated_at = ?
        WHERE agent_id = ?
    """, (float(amount), float(amount), now_iso(), agent_id))
    return True

# ---------------------------------------------------------------------------
# Spend tracking helpers
# ---------------------------------------------------------------------------

def todays_spend(agent_id: int) -> float:
    start = now_dt().replace(hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")
    result = query_df("""
        SELECT COALESCE(SUM(amount), 0) AS total
        FROM transactions
        WHERE agent_id = ? AND status = 'approved' AND created_at >= ?
    """, (agent_id, start))
    return float(result.iloc[0]["total"])

def txs_last_minute(agent_id: int) -> Tuple[int, float]:
    cutoff = (now_dt() - timedelta(seconds=60)).isoformat(timespec="seconds")
    result = query_df("""
        SELECT
            COALESCE(SUM(CASE WHEN source != 'stream' THEN 1 ELSE 0 END), 0) AS tx_count,
            COALESCE(SUM(amount), 0) AS spend
        FROM transactions
        WHERE agent_id = ? AND created_at >= ?
    """, (agent_id, cutoff))
    return int(result.iloc[0]["tx_count"]), float(result.iloc[0]["spend"])

def create_alert(agent_id: int, severity: str, message: str):
    alert_id = execute("""
        INSERT INTO alerts (agent_id, severity, message, created_at)
        VALUES (?, ?, ?, ?)
    """, (agent_id, severity, message, now_iso()))
    # v5: optional real Slack webhook for critical alerts (set SLACK_WEBHOOK_URL env var)
    if severity == "critical":
        webhook = os.environ.get("SLACK_WEBHOOK_URL")
        if webhook:
            try:
                import requests as _requests
                agent = get_agent(agent_id) or {"name": f"agent#{agent_id}"}
                _requests.post(webhook, json={
                    "text": f":rotating_light: *AI Wallet Guard — CRITICAL*\nAgent: {agent['name']}\n{message}"
                }, timeout=2)
            except Exception:
                pass  # alerting must never break the payment path
    return alert_id

def resolve_review(tx_id: int, approve: bool, reviewer_note: str = "") -> Dict[str, Any]:
    """Human-in-the-loop: approve or reject a transaction sitting in the review queue."""
    row = fetchone("SELECT * FROM transactions WHERE id = ?", (tx_id,))
    if not row:
        raise ValueError("Transaction not found")
    tx = row
    if tx["status"] != "review":
        raise ValueError(f"Transaction #{tx_id} is '{tx['status']}', not in review.")

    if approve:
        if not debit_wallet(tx["agent_id"], tx["amount"]):
            new_status, note = "blocked", "Human approved, but wallet balance was insufficient."
        else:
            new_status, note = "approved", f"Approved by human reviewer. {reviewer_note}".strip()
    else:
        new_status, note = "blocked", f"Rejected by human reviewer. {reviewer_note}".strip()

    execute("""
        UPDATE transactions SET status = ?, decision_reason = decision_reason || ' | ' || ?
        WHERE id = ?
    """, (new_status, note, tx_id))
    create_alert(tx["agent_id"], "info", f"Review resolved: transaction #{tx_id} → {new_status}. {note}")
    return {"transaction_id": tx_id, "status": new_status, "note": note}

# ---------------------------------------------------------------------------
# Risk engine (rules + statistical anomaly detection)
# ---------------------------------------------------------------------------

RISKY_KEYWORDS = [
    "ignore previous", "bypass", "jailbreak", "send all", "private key",
    "seed phrase", "withdraw", "unlimited", "admin override", "crypto transfer",
    "disable limit", "turn off policy", "keep buying", "do not stop"
]
HIGH_RISK_CATEGORIES = ["unknown", "crypto", "wallet", "external transfer"]

def amount_anomaly(agent_id: int, amount: float) -> Optional[str]:
    """z-score style anomaly: amount far above this agent's own historical baseline."""
    hist = query_df("""
        SELECT amount FROM transactions
        WHERE agent_id = ? AND status = 'approved' AND source != 'stream'
        ORDER BY id DESC LIMIT 50
    """, (agent_id,))
    values: List[float] = hist["amount"].tolist()
    if len(values) < 5:
        return None
    mean = statistics.mean(values)
    stdev = statistics.pstdev(values)
    threshold = mean + 3 * max(stdev, mean * 0.25, 0.01)
    if amount > threshold:
        return (f"Anomaly detected: ${amount:.4f} is far above this agent's historical "
                f"baseline (mean ${mean:.4f} over last {len(values)} approved txs).")
    return None

def is_new_merchant(agent_id: int, merchant: str) -> bool:
    result = query_df("""
        SELECT COUNT(*) AS c FROM transactions
        WHERE agent_id = ? AND LOWER(merchant) = LOWER(?)
    """, (agent_id, merchant))
    return int(result.iloc[0]["c"]) == 0

def _f(v) -> float:
    """Cast Decimal (Postgres NUMERIC) to float safely."""
    return float(v) if v is not None else 0.0


def score_transaction(agent: Dict[str, Any], amount: float, merchant: str, category: str, reason: str):
    score = 0
    hard_block = False
    reasons = []
    amount = float(amount)

    if agent["status"] != "active":
        return 100, True, "Agent is currently frozen. Request denied immediately."

    if amount > _f(_f(agent["per_tx_limit"])):
        score += 70
        hard_block = True
        reasons.append(f"Amount ${amount:.4f} exceeds per-transaction limit ${agent['per_tx_limit']:.4f}.")

    spent_today = todays_spend(agent["id"])
    if spent_today + amount > _f(agent["daily_budget"]):
        score += 70
        hard_block = True
        reasons.append(f"Daily budget would be exceeded: ${spent_today + amount:.4f} / ${_f(agent['daily_budget']):.4f}.")

    tx_count, spend_60s = txs_last_minute(agent["id"])
    if tx_count + 1 > int(agent["max_txs_per_minute"]):
        score += 45
        reasons.append(f"Transaction velocity too high: {tx_count + 1} txs in 60s; limit is {int(agent['max_txs_per_minute'])}.")

    if spend_60s + amount > _f(agent["per_minute_spend_limit"]):
        score += 45
        hard_block = True
        reasons.append(f"Spend velocity too high: ${spend_60s + amount:.4f} in 60s; limit is ${_f(agent['per_minute_spend_limit']):.4f}.")

    # v4: statistical anomaly detection
    anomaly_msg = amount_anomaly(agent["id"], amount)
    if anomaly_msg:
        score += 40
        reasons.append(anomaly_msg)

    if is_new_merchant(agent["id"], merchant) and amount > _f(agent["per_tx_limit"]) * 0.5:
        score += 15
        reasons.append(f"First-time merchant '{merchant}' with a relatively large amount.")

    text = f"{merchant} {category} {reason}".lower()
    triggered_keywords = [k for k in RISKY_KEYWORDS if k in text]
    if triggered_keywords:
        score += 50
        reasons.append("Prompt/payment description contains risky keywords: " + ", ".join(triggered_keywords[:3]) + ".")

    if category.lower() in HIGH_RISK_CATEGORIES:
        score += 20
        reasons.append(f"High-risk or unclear category: {category}.")

    if not reasons:
        reasons.append("Approved. Transaction is within policy.")

    return min(score, 100), hard_block, " ".join(reasons)

# ---------------------------------------------------------------------------
# Authorization (now wallet-aware)
# ---------------------------------------------------------------------------

def record_transaction(agent_id, merchant, category, amount, reason, status, risk_score, decision_reason, source):
    return execute("""
        INSERT INTO transactions
        (agent_id, merchant, category, amount, reason, status, risk_score, decision_reason, source, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (agent_id, merchant, category, float(amount), reason, status, risk_score, decision_reason, source, now_iso()))

def authorize_payment(agent_id: int, amount: float, merchant: str, category: str, reason: str, source: str = "manual"):
    agent = get_agent(agent_id)
    if not agent:
        raise ValueError("Agent not found")

    risk_score, hard_block, decision_reason = score_transaction(agent, amount, merchant, category, reason)

    # frozen agents that keep sending requests get an extra security audit trail
    if agent["status"] == "frozen":
        tx_id = record_transaction(agent_id, merchant, category, amount, reason,
                                   "blocked", risk_score, decision_reason, source)
        create_alert(agent_id, "critical",
                     f"SECURITY AUDIT: Frozen agent attempted transaction #{tx_id}. "
                     f"Possible malicious loop or hijacked prompt.")
        return format_response(tx_id, agent, amount, merchant, category, "blocked", risk_score, decision_reason)

    if hard_block or risk_score >= 70:
        status = "blocked"
        severity = "critical"
    elif risk_score >= 40:
        status = "review"
        severity = "warning"
    else:
        status = "approved"
        severity = None

    # v4: wallet check — even a policy-clean payment fails without funds
    if status == "approved":
        wallet = get_wallet(agent_id)
        if not wallet or wallet["balance"] < amount:
            status = "blocked"
            severity = "critical"
            risk_score = max(risk_score, 60)
            decision_reason += f" Insufficient wallet balance (${(wallet or {}).get('balance', 0):.4f} available)."

    tx_id = record_transaction(agent_id, merchant, category, amount, reason,
                               status, risk_score, decision_reason, source)

    if status == "approved":
        debit_wallet(agent_id, amount)

    if status in ("blocked", "review"):
        create_alert(agent_id, severity, f"Transaction #{tx_id} marked as {status}. {decision_reason}")

    # circuit breaker + automatic yield routing of unused budget
    if status == "blocked" and risk_score >= 70 and agent["status"] == "active":
        execute("UPDATE agents SET status = 'frozen' WHERE id = ?", (agent_id,))
        create_alert(agent_id, "critical", "Circuit breaker triggered. Agent has been frozen automatically.")
        sweep_idle_funds(agent_id, reason="circuit_breaker_freeze")

    updated_agent = get_agent(agent_id)
    return format_response(tx_id, updated_agent, amount, merchant, category, status, risk_score, decision_reason)

def format_response(tx_id, agent, amount, merchant, category, status, risk_score, decision_reason):
    wallet = get_wallet(agent["id"]) or {}
    return {
        "transaction_id": tx_id,
        "agent": agent["name"],
        "agent_status": agent["status"],
        "amount": float(amount),
        "merchant": merchant,
        "category": category,
        "status": status,
        "risk_score": risk_score,
        "decision_reason": decision_reason,
        "wallet_balance": round(float(wallet.get("balance", 0)), 4),
    }

# ---------------------------------------------------------------------------
# Yield vault primitives (simulated Morpho / Aave / tokenized T-bills)
# ---------------------------------------------------------------------------

YIELD_PROTOCOLS = {
    "Morpho":        {"apy": 0.052, "type": "DeFi lending"},
    "Aave":          {"apy": 0.038, "type": "DeFi lending"},
    "T-Bill RWA":    {"apy": 0.049, "type": "Tokenized treasuries"},
}

def pick_best_protocol() -> str:
    return max(YIELD_PROTOCOLS, key=lambda k: YIELD_PROTOCOLS[k]["apy"])

def sweep_idle_funds(agent_id: int, protocol: Optional[str] = None, reason: str = "manual_sweep") -> Optional[Dict[str, Any]]:
    """
    Move wallet balance above the operating reserve into a yield position.
    Reserve = the agent's daily_budget (so day-to-day spending is never affected).
    """
    agent = get_agent(agent_id)
    wallet = get_wallet(agent_id)
    if not agent or not wallet:
        return None

    reserve = float(_f(agent["daily_budget"]))
    idle = round(wallet["balance"] - reserve, 4)
    if idle <= 0.01:
        return None

    protocol = protocol or pick_best_protocol()
    apy = YIELD_PROTOCOLS[protocol]["apy"]

    execute("""
        UPDATE wallets SET balance = balance - ?, updated_at = ? WHERE agent_id = ?
    """, (idle, now_iso(), agent_id))
    position_id = execute("""
        INSERT INTO yield_positions (agent_id, protocol, principal, apy, accrued_interest, status, created_at, last_accrued_at)
        VALUES (?, ?, ?, ?, 0, 'active', ?, ?)
    """, (agent_id, protocol, idle, apy, now_iso(), now_iso()))

    create_alert(agent_id, "info",
                 f"YIELD ROUTING ({reason}): ${idle:.4f} idle funds swept into {protocol} "
                 f"at {apy*100:.1f}% APY. Operating reserve ${reserve:.2f} kept in wallet.")
    return {"position_id": position_id, "protocol": protocol, "amount": idle, "apy": apy}

def accrue_yield():
    """Accrue interest on all active positions based on elapsed time (simulated, accelerated x10000 for demo)."""
    DEMO_ACCELERATION = 10000  # 1 real second ≈ 2.8 simulated hours, so demos show visible interest
    positions = query_df("SELECT * FROM yield_positions WHERE status = 'active'")
    for _, p in positions.iterrows():
        last = datetime.fromisoformat(p["last_accrued_at"])
        elapsed_seconds = max((now_dt() - last).total_seconds(), 0)
        year_fraction = (elapsed_seconds * DEMO_ACCELERATION) / (365 * 24 * 3600)
        interest = float(p["principal"]) * float(p["apy"]) * year_fraction
        if interest > 0:
            execute("""
                UPDATE yield_positions
                SET accrued_interest = accrued_interest + ?, last_accrued_at = ?
                WHERE id = ?
            """, (interest, now_iso(), int(p["id"])))

def withdraw_yield(agent_id: int) -> Dict[str, float]:
    """Close all active positions and return principal + interest to the wallet."""
    accrue_yield()
    positions = query_df("SELECT * FROM yield_positions WHERE agent_id = ? AND status = 'active'", (agent_id,))
    total_principal = float(positions["principal"].sum()) if not positions.empty else 0.0
    total_interest = float(positions["accrued_interest"].sum()) if not positions.empty else 0.0
    total = total_principal + total_interest
    if total > 0:
        execute("""
            UPDATE yield_positions SET status = 'withdrawn', withdrawn_at = ?
            WHERE agent_id = ? AND status = 'active'
        """, (now_iso(), agent_id))
        credit_wallet(agent_id, total)
        create_alert(agent_id, "info",
                     f"YIELD WITHDRAWN: ${total_principal:.4f} principal + ${total_interest:.4f} interest returned to wallet.")
    return {"principal": round(total_principal, 4), "interest": round(total_interest, 6), "total": round(total, 4)}
