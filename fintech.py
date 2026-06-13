"""
AI Wallet Guard v4 — FinTech modules built on top of storage.py

1. Streaming Payments Gateway (Pay-As-You-Go AI Gateway)
   計時/計量型微支付: per_second / per_call / per_token metered streams.
   Money flows with the data — each tick is checked, debited, and audited.

2. Machine Credit Score (機器信用分)
   0-1000 score computed from the agent's full transaction history.
   Higher tiers earn higher effective per-tx limits (simulated perk).

3. Tax & Compliance Engine (實時合規與稅務解析)
   Auto-classifies machine-speed transactions and generates per-jurisdiction
   tax estimates (US / EU / TW). Demo estimates only — not tax advice.
"""

from datetime import datetime, timezone
from typing import Optional, Dict, Any

import pandas as pd

from storage import (
    query_df, execute, now_iso,
    get_agent, get_wallet, debit_wallet,
    todays_spend, txs_last_minute,
    record_transaction, create_alert,
)

# ===========================================================================
# 1. STREAMING PAYMENTS GATEWAY
# ===========================================================================

VALID_UNIT_TYPES = ("per_second", "per_call", "per_token")

def start_stream(agent_id: int, provider: str, unit_type: str, unit_price: float) -> Dict[str, Any]:
    agent = get_agent(agent_id)
    if not agent:
        raise ValueError("Agent not found")
    if unit_type not in VALID_UNIT_TYPES:
        raise ValueError(f"unit_type must be one of {VALID_UNIT_TYPES}")
    if agent["status"] != "active":
        raise PermissionError(f"Agent '{agent['name']}' is frozen. Stream refused.")

    wallet = get_wallet(agent_id)
    if not wallet or wallet["balance"] <= 0:
        raise PermissionError("Wallet has no funds. Stream refused.")

    stream_id = execute("""
        INSERT INTO streams (agent_id, provider, unit_type, unit_price, started_at)
        VALUES (?, ?, ?, ?, ?)
    """, (agent_id, provider, unit_type, float(unit_price), now_iso()))

    create_alert(agent_id, "info",
                 f"STREAM OPENED #{stream_id}: {provider} at ${unit_price:.6f} {unit_type}.")
    return get_stream(stream_id)

def get_stream(stream_id: int) -> Optional[Dict[str, Any]]:
    rows = query_df("SELECT * FROM streams WHERE id = ?", (stream_id,))
    return rows.iloc[0].to_dict() if not rows.empty else None

def stream_tick(stream_id: int, units: float) -> Dict[str, Any]:
    """
    Meter one slice of usage. Cost = units * unit_price.
    Guard checks on EVERY tick: frozen status, daily budget, per-minute
    spend velocity, wallet balance. Any violation terminates the stream.
    """
    stream = get_stream(stream_id)
    if not stream:
        raise ValueError("Stream not found")
    if stream["status"] != "open":
        return {**stream, "tick_status": "rejected", "tick_reason": f"Stream is {stream['status']}."}

    agent = get_agent(int(stream["agent_id"]))
    cost = round(float(units) * float(stream["unit_price"]), 6)

    # --- guard checks (every tick) ---
    violation = None
    if agent["status"] != "active":
        violation = "Agent was frozen mid-stream."
    elif todays_spend(agent["id"]) + cost > agent["daily_budget"]:
        violation = "Daily budget would be exceeded by this tick."
    else:
        _, spend_60s = txs_last_minute(agent["id"])
        if spend_60s + cost > agent["per_minute_spend_limit"]:
            violation = (f"Spend velocity too high: ${spend_60s + cost:.4f} in 60s; "
                         f"limit is ${agent['per_minute_spend_limit']:.4f}.")
        elif not debit_wallet(agent["id"], cost):
            violation = "Insufficient wallet balance."

    if violation:
        _close_stream(stream_id, "terminated", violation)
        record_transaction(agent["id"], stream["provider"], "stream", cost,
                           f"Stream #{stream_id} tick rejected", "blocked", 75, violation, "stream")
        create_alert(agent["id"], "critical",
                     f"STREAM TERMINATED #{stream_id}: {violation}")
        return {**get_stream(stream_id), "tick_status": "blocked", "tick_reason": violation}

    # --- success: money flows with the data ---
    record_transaction(agent["id"], stream["provider"], "stream", cost,
                       f"Stream #{stream_id}: {units} {stream['unit_type']} units",
                       "approved", 0, "Streaming micro-payment within policy.", "stream")
    execute("""
        UPDATE streams SET units_consumed = units_consumed + ?, total_cost = total_cost + ?
        WHERE id = ?
    """, (float(units), cost, stream_id))

    return {**get_stream(stream_id), "tick_status": "approved", "tick_cost": cost,
            "wallet_balance": round(get_wallet(agent["id"])["balance"], 4)}

def stop_stream(stream_id: int) -> Dict[str, Any]:
    stream = get_stream(stream_id)
    if not stream:
        raise ValueError("Stream not found")
    if stream["status"] == "open":
        _close_stream(stream_id, "closed", "Closed normally by agent.")
        create_alert(int(stream["agent_id"]), "info",
                     f"STREAM CLOSED #{stream_id}: total ${stream['total_cost']:.4f} "
                     f"for {stream['units_consumed']:.1f} units.")
    return get_stream(stream_id)

def _close_stream(stream_id: int, status: str, reason: str):
    execute("""
        UPDATE streams SET status = ?, close_reason = ?, ended_at = ?
        WHERE id = ?
    """, (status, reason, now_iso(), stream_id))

# ===========================================================================
# 2. MACHINE CREDIT SCORE
# ===========================================================================

CREDIT_TIERS = [
    (800, "Platinum", 2.0, "Trusted agent: 2x effective per-tx limit, lowest fees"),
    (650, "Gold",     1.5, "Reliable agent: 1.5x effective per-tx limit"),
    (500, "Silver",   1.0, "Standard agent: normal limits"),
    (0,   "Watchlist", 0.5, "Risky agent: limits reduced to 0.5x, manual review encouraged"),
]

def compute_credit_score(agent_id: int) -> Dict[str, Any]:
    agent = get_agent(agent_id)
    if not agent:
        raise ValueError("Agent not found")

    txs = query_df("""
        SELECT status, COUNT(*) AS c FROM transactions
        WHERE agent_id = ? GROUP BY status
    """, (agent_id,))
    counts = dict(zip(txs["status"], txs["c"])) if not txs.empty else {}
    approved = int(counts.get("approved", 0))
    blocked = int(counts.get("blocked", 0))
    review = int(counts.get("review", 0))
    total = approved + blocked + review

    score = 500.0
    factors = []

    if total == 0:
        factors.append("No history yet: neutral starting score (thin file).")
    else:
        approval_rate = approved / total
        blocked_rate = blocked / total
        score += approval_rate * 300
        score -= blocked_rate * 400
        score -= (review / total) * 100
        factors.append(f"Approval rate {approval_rate*100:.0f}% over {total} txs.")
        if blocked:
            factors.append(f"{blocked} blocked txs ({blocked_rate*100:.0f}%) drag the score down.")

    # longevity bonus
    created = datetime.fromisoformat(agent["created_at"])
    age_days = max((datetime.now(timezone.utc) - created).days, 0)
    longevity = min(age_days * 5, 100)
    score += longevity
    if longevity:
        factors.append(f"Account age {age_days} days: +{longevity} longevity bonus.")

    # frozen penalty
    if agent["status"] == "frozen":
        score -= 150
        factors.append("Agent is currently FROZEN: -150.")

    # earning behavior bonus (agents that earn are skin-in-the-game)
    wallet = get_wallet(agent_id) or {}
    if wallet.get("total_earned", 0) > 0:
        score += 50
        factors.append(f"Revenue-generating agent (${wallet['total_earned']:.2f} earned): +50.")

    # v5: verified digital identity bonus
    ident = query_df("SELECT verification_level, did FROM identities WHERE agent_id = ?", (agent_id,))
    if not ident.empty:
        level = ident.iloc[0]["verification_level"]
        if level == "zk_verified":
            score += 75
            factors.append(f"ZK-verified owner identity ({ident.iloc[0]['did']}): +75.")
        else:
            score += 25
            factors.append(f"Registered DID ({ident.iloc[0]['did']}): +25.")

    score = int(max(0, min(1000, round(score))))

    for threshold, tier, multiplier, perk in CREDIT_TIERS:
        if score >= threshold:
            return {
                "agent": agent["name"],
                "score": score,
                "tier": tier,
                "limit_multiplier": multiplier,
                "effective_per_tx_limit": round(agent["per_tx_limit"] * multiplier, 4),
                "perk": perk,
                "factors": factors,
            }

# ===========================================================================
# 3. TAX & COMPLIANCE ENGINE
# ===========================================================================

# category → compliance classification
TAX_CLASSIFICATION = {
    "data api":          ("Digital services / data licensing", "海外勞務 — 數位服務"),
    "compute":           ("Cloud infrastructure (IaaS)", "雲端基礎設施"),
    "image generation":  ("Digital services / content generation", "數位內容服務"),
    "database":          ("Software / SaaS subscription", "軟體授權"),
    "software":          ("Software licensing", "軟體授權"),
    "stream":            ("Metered digital services", "計量型數位服務"),
    "crypto":            ("Digital asset transfer (flagged)", "虛擬資產移轉 (標記)"),
    "external transfer": ("Funds transfer (flagged)", "資金移轉 (標記)"),
    "unknown":           ("Unclassified (manual review)", "未分類 (人工覆核)"),
}

JURISDICTIONS = {
    "US": {"label": "United States", "rate": 0.00,
           "note": "B2B digital services generally not subject to federal sales tax; state nexus rules vary. 1099/withholding may apply to some vendors."},
    "EU": {"label": "European Union", "rate": 0.21,
           "note": "Reverse-charge VAT (~21% avg) self-assessed on imported digital services by the business buyer."},
    "TW": {"label": "Taiwan 台灣", "rate": 0.05,
           "note": "境外電商勞務 5% 營業稅 (reverse charge); 部分海外勞務另涉 20% 扣繳，需個案認定。"},
}

def generate_tax_report(jurisdiction: str = "TW", use_ai: bool = True) -> Dict[str, Any]:
    if jurisdiction not in JURISDICTIONS:
        raise ValueError(f"jurisdiction must be one of {list(JURISDICTIONS)}")
    juris = JURISDICTIONS[jurisdiction]

    txs = query_df("""
        SELECT t.id, t.created_at, a.name AS agent, t.merchant, t.category, t.amount, t.reason, t.source
        FROM transactions t JOIN agents a ON t.agent_id = a.id
        WHERE t.status = 'approved'
        ORDER BY t.id
    """)
    if txs.empty:
        return {"jurisdiction": juris["label"], "note": juris["note"],
                "line_items": pd.DataFrame(), "summary": pd.DataFrame(),
                "total_spend": 0.0, "total_estimated_tax": 0.0}

    def classify_rule(cat):
        return TAX_CLASSIFICATION.get(cat.lower(), TAX_CLASSIFICATION["unknown"])

    if use_ai:
        # v5: self-learning classifier — confident predictions are used directly;
        # uncertain ones fall back to the rule table and are flagged for review.
        from tax_ai import classify as ai_classify, CONFIDENCE_THRESHOLD
        labels, confidences, sources, reviews = [], [], [], []
        for _, t in txs.iterrows():
            pred = ai_classify(t["merchant"], t["category"], t["reason"] or "")
            if pred["confidence"] >= CONFIDENCE_THRESHOLD:
                labels.append(pred["label"])
                sources.append("ai_model")
            else:
                labels.append(classify_rule(t["category"])[0])
                sources.append("rule_fallback")
            confidences.append(pred["confidence"])
            reviews.append(pred["needs_review"])
        txs["tax_category"] = labels
        txs["ai_confidence"] = confidences
        txs["classified_by"] = sources
        txs["needs_review"] = reviews
    else:
        txs["tax_category"] = txs["category"].map(lambda c: classify_rule(c)[0])
        txs["ai_confidence"] = None
        txs["classified_by"] = "rule"
        txs["needs_review"] = txs["category"].str.lower().isin(["unknown", "crypto", "external transfer"])

    txs["tax_rate"] = juris["rate"]
    txs["estimated_tax"] = (txs["amount"] * juris["rate"]).round(6)

    summary = (txs.groupby("tax_category")
                  .agg(tx_count=("id", "count"),
                       total_amount=("amount", "sum"),
                       estimated_tax=("estimated_tax", "sum"))
                  .reset_index()
                  .sort_values("total_amount", ascending=False))

    return {
        "jurisdiction": juris["label"],
        "note": juris["note"],
        "line_items": txs,
        "summary": summary,
        "total_spend": round(float(txs["amount"].sum()), 4),
        "total_estimated_tax": round(float(txs["estimated_tax"].sum()), 4),
        "review_count": int(txs["needs_review"].sum()),
    }
