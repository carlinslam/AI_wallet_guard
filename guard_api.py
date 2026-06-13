"""
AI Wallet Guard v4 — Guard API

Endpoints:
- POST /authorize-payment              one-shot payment authorization (wallet-aware)
- GET  /agent-status/{name}            lightweight pre-flight check
- GET  /agents                         list agents

Wallets (虛擬子卡):
- GET  /wallet/{name}                  wallet balance + earned/spent
- POST /wallet/{name}/earn             agent receives revenue (simulated income)

Streaming Gateway (Pay-As-You-Go):
- POST /gateway/start-stream           open a metered stream (per_second / per_call / per_token)
- POST /gateway/stream/{id}/tick       meter usage; guard-checked + debited per tick
- POST /gateway/stream/{id}/stop       close stream
- GET  /gateway/streams                list streams

Credit:
- GET  /credit-score/{name}            machine credit score 0-1000 + tier + perks

Tax & Compliance:
- GET  /tax-report?jurisdiction=TW     auto-classified tax estimate (US / EU / TW)

Yield Vault:
- POST /yield/sweep/{name}             sweep idle funds above operating reserve into yield
- POST /yield/withdraw/{name}          close positions, return principal + interest
- GET  /yield/positions                list positions (interest accrued on read)
"""

from typing import Optional
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from storage import (
    seed_data,
    reset_demo_data,
    get_agent_by_name,
    get_wallet,
    credit_wallet,
    authorize_payment,
    sweep_idle_funds,
    withdraw_yield,
    accrue_yield,
    record_transaction,
    create_alert,
    resolve_review,
)
from db import query_df as df, fetchone
from fintech import (
    start_stream,
    stream_tick,
    stop_stream,
    compute_credit_score,
    generate_tax_report,
)
from identity import (
    register_identity,
    get_identity,
    verify_signature,
    verify_owner,
    set_require_signature,
    list_identities,
)
from tax_ai import classify as tax_classify, add_human_label, training_stats, invalidate_model, TAX_CATEGORIES
from auth import (
    bootstrap_root_key, issue_api_key, rotate_api_key, revoke_api_key, list_api_keys,
    require_scope,
)
from solana_rail import (
    settle_payment, list_payments, treasury_info, TREASURY_PUBKEY,
)

app = FastAPI(
    title="AI Wallet Guard API",
    description="Financial firewall + FinTech rails for autonomous AI agents. Demo only — no real money moves.",
    version="0.7.0",
)

class PaymentRequest(BaseModel):
    agent_name: str = Field(..., examples=["Research Agent"])
    amount: float = Field(..., gt=0, examples=[0.05])
    merchant: str = Field(..., examples=["PaidDataAPI"])
    category: str = Field(..., examples=["data api"])
    reason: str = Field(..., examples=["Need to buy one paid search result for research."])
    task_id: Optional[str] = Field(None, examples=["task_001"])
    signature: Optional[str] = Field(None, description="HMAC-SHA256 over the canonical payload (see SDK)")

class RegisterIdentityRequest(BaseModel):
    agent_name: str = Field(..., examples=["Research Agent"])
    owner_entity: str = Field(..., examples=["Acme Robotics Inc."])
    require_signature: bool = Field(False)

class VerifyOwnerRequest(BaseModel):
    agent_name: str
    owner_entity: str
    owner_nonce: str

class TaxClassifyRequest(BaseModel):
    merchant: str = Field(..., examples=["GPU-Rent-Node"])
    category: str = Field(..., examples=["compute"])
    reason: str = Field("", examples=["Rent GPU for fine-tuning"])

class TaxFeedbackRequest(BaseModel):
    merchant: str
    category: str
    reason: str = ""
    label: str = Field(..., description="The correct tax category")

class ReviewResolveRequest(BaseModel):
    approve: bool
    note: str = ""

class IssueApiKeyRequest(BaseModel):
    name: str = Field(..., examples=["prod-key"])
    scopes: str = Field("read,write", examples=["read,write"])

class SettleRequest(BaseModel):
    payee_pubkey: str = Field(..., description="Solana wallet address of the payee")
    network: str = Field("devnet", examples=["devnet"])

class EarnRequest(BaseModel):
    amount: float = Field(..., gt=0, examples=[0.002])
    source: str = Field("plugin_sale", examples=["plugin_sale"])

class StartStreamRequest(BaseModel):
    agent_name: str = Field(..., examples=["Image Agent"])
    provider: str = Field(..., examples=["GPU-Rent-Node"])
    unit_type: str = Field(..., examples=["per_second"])
    unit_price: float = Field(..., gt=0, examples=[0.001])

class TickRequest(BaseModel):
    units: float = Field(..., gt=0, examples=[1])

def _require_agent(agent_name: str):
    agent = get_agent_by_name(agent_name)
    if not agent:
        raise HTTPException(status_code=404, detail=f"Agent '{agent_name}' not found.")
    return agent

@app.on_event("startup")
def startup():
    seed_data()
    bootstrap_root_key()

@app.get("/")
def root():
    return {"message": "AI Wallet Guard v4. POST /authorize-payment before an AI agent spends money. See /docs."}

@app.get("/health")
def health():
    return {"status": "ok", "version": "0.7.0"}

# ---------------------------------------------------------------------------
# Core authorization
# ---------------------------------------------------------------------------

@app.get("/agent-status/{agent_name}")
def get_agent_status(agent_name: str):
    agent = _require_agent(agent_name)
    return {"agent_name": agent["name"], "status": agent["status"]}

@app.get("/agents")
def list_agents():
    agents = df("""
        SELECT id, name, purpose, daily_budget, per_tx_limit, per_minute_spend_limit,
               max_txs_per_minute, status, created_at
        FROM agents ORDER BY id
    """)
    return agents.to_dict(orient="records")

@app.post("/authorize-payment")
def authorize_payment_endpoint(payload: PaymentRequest, _key=Depends(require_scope("write"))):
    agent = _require_agent(payload.agent_name)

    # v5: identity / signature enforcement BEFORE the risk engine
    sig = verify_signature(agent["id"], payload.agent_name, payload.amount,
                           payload.merchant, payload.category, payload.reason,
                           payload.signature)
    if not sig["ok"]:
        tx_id = record_transaction(agent["id"], payload.merchant, payload.category,
                                   payload.amount, payload.reason, "blocked", 95,
                                   f"IDENTITY CHECK FAILED: {sig['detail']}", "ai_agent_api")
        create_alert(agent["id"], "critical",
                     f"IDENTITY CHECK FAILED on transaction #{tx_id}: {sig['detail']}")
        return {
            "transaction_id": tx_id, "agent": agent["name"], "agent_status": agent["status"],
            "amount": payload.amount, "merchant": payload.merchant, "category": payload.category,
            "status": "blocked", "risk_score": 95,
            "decision_reason": f"IDENTITY CHECK FAILED: {sig['detail']}",
            "wallet_balance": round(float((get_wallet(agent['id']) or {}).get('balance', 0)), 4),
            "allowed_to_spend": False, "task_id": payload.task_id,
            "signature_check": sig,
        }

    result = authorize_payment(
        agent_id=agent["id"],
        amount=payload.amount,
        merchant=payload.merchant,
        category=payload.category,
        reason=payload.reason,
        source="ai_agent_api",
    )
    result["allowed_to_spend"] = result["status"] == "approved"
    result["task_id"] = payload.task_id
    result["signature_check"] = sig
    return result

# ---------------------------------------------------------------------------
# Wallets
# ---------------------------------------------------------------------------

@app.get("/wallet/{agent_name}")
def wallet_endpoint(agent_name: str):
    agent = _require_agent(agent_name)
    wallet = get_wallet(agent["id"])
    return {"agent": agent["name"],
            "balance": round(wallet["balance"], 4),
            "total_earned": round(wallet["total_earned"], 4),
            "total_spent": round(wallet["total_spent"], 4),
            "updated_at": wallet["updated_at"]}

@app.post("/wallet/{agent_name}/earn")
def earn_endpoint(agent_name: str, payload: EarnRequest):
    agent = _require_agent(agent_name)
    credit_wallet(agent["id"], payload.amount)
    wallet = get_wallet(agent["id"])
    return {"agent": agent["name"], "earned": payload.amount, "source": payload.source,
            "balance": round(wallet["balance"], 4)}

# ---------------------------------------------------------------------------
# Streaming Payments Gateway
# ---------------------------------------------------------------------------

@app.post("/gateway/start-stream")
def start_stream_endpoint(payload: StartStreamRequest):
    agent = _require_agent(payload.agent_name)
    try:
        return start_stream(agent["id"], payload.provider, payload.unit_type, payload.unit_price)
    except (PermissionError, ValueError) as exc:
        raise HTTPException(status_code=403, detail=str(exc))

@app.post("/gateway/stream/{stream_id}/tick")
def tick_endpoint(stream_id: int, payload: TickRequest):
    try:
        return stream_tick(stream_id, payload.units)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

@app.post("/gateway/stream/{stream_id}/stop")
def stop_stream_endpoint(stream_id: int):
    try:
        return stop_stream(stream_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

@app.get("/gateway/streams")
def list_streams():
    streams = df("""
        SELECT s.*, a.name AS agent
        FROM streams s JOIN agents a ON s.agent_id = a.id
        ORDER BY s.id DESC
    """)
    return streams.to_dict(orient="records")

# ---------------------------------------------------------------------------
# Machine Credit Score
# ---------------------------------------------------------------------------

@app.get("/credit-score/{agent_name}")
def credit_score_endpoint(agent_name: str):
    agent = _require_agent(agent_name)
    return compute_credit_score(agent["id"])

# ---------------------------------------------------------------------------
# Tax & Compliance
# ---------------------------------------------------------------------------

@app.get("/tax-report")
def tax_report_endpoint(jurisdiction: str = "TW"):
    try:
        report = generate_tax_report(jurisdiction.upper())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "jurisdiction": report["jurisdiction"],
        "note": report["note"],
        "total_spend": report["total_spend"],
        "total_estimated_tax": report["total_estimated_tax"],
        "items_needing_manual_review": report.get("review_count", 0),
        "summary": report["summary"].to_dict(orient="records") if not report["summary"].empty else [],
        "disclaimer": "Demo estimates only. Not tax, legal, or accounting advice.",
    }

# ---------------------------------------------------------------------------
# Yield Vault
# ---------------------------------------------------------------------------

@app.post("/yield/sweep/{agent_name}")
def yield_sweep_endpoint(agent_name: str, _key=Depends(require_scope("write"))):
    agent = _require_agent(agent_name)
    result = sweep_idle_funds(agent["id"], reason="api_sweep")
    if not result:
        return {"agent": agent["name"], "swept": 0,
                "message": "No idle funds above the operating reserve (daily budget)."}
    return {"agent": agent["name"], **result}

@app.post("/yield/withdraw/{agent_name}")
def yield_withdraw_endpoint(agent_name: str):
    agent = _require_agent(agent_name)
    return {"agent": agent["name"], **withdraw_yield(agent["id"])}

@app.get("/yield/positions")
def yield_positions_endpoint():
    accrue_yield()
    positions = df("""
        SELECT y.*, a.name AS agent
        FROM yield_positions y JOIN agents a ON y.agent_id = a.id
        ORDER BY y.id DESC
    """)
    return positions.to_dict(orient="records")

# ---------------------------------------------------------------------------
# Digital Identity (DeID)
# ---------------------------------------------------------------------------

@app.post("/identity/register")
def register_identity_endpoint(payload: RegisterIdentityRequest, _key=Depends(require_scope("write"))):
    agent = _require_agent(payload.agent_name)
    try:
        return register_identity(agent["id"], payload.owner_entity, payload.require_signature)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@app.post("/identity/verify-owner")
def verify_owner_endpoint(payload: VerifyOwnerRequest):
    agent = _require_agent(payload.agent_name)
    try:
        return verify_owner(agent["id"], payload.owner_entity, payload.owner_nonce)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@app.post("/identity/{agent_name}/require-signature/{required}")
def require_signature_endpoint(agent_name: str, required: bool):
    agent = _require_agent(agent_name)
    if not get_identity(agent["id"]):
        raise HTTPException(status_code=400, detail="Agent has no identity. Register first.")
    set_require_signature(agent["id"], required)
    return {"agent": agent["name"], "require_signature": required}

@app.get("/identity")
def list_identities_endpoint():
    return list_identities().to_dict(orient="records")

# ---------------------------------------------------------------------------
# Self-learning tax classifier
# ---------------------------------------------------------------------------

@app.post("/tax/classify")
def tax_classify_endpoint(payload: TaxClassifyRequest):
    return tax_classify(payload.merchant, payload.category, payload.reason)

@app.post("/tax/feedback")
def tax_feedback_endpoint(payload: TaxFeedbackRequest, _key=Depends(require_scope("write"))):
    try:
        label_id = add_human_label(payload.merchant, payload.category, payload.reason, payload.label)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"label_id": label_id, "stored": True, "model": "retrained",
            "training_examples": training_stats(),
            "new_prediction": tax_classify(payload.merchant, payload.category, payload.reason)}

@app.get("/tax/categories")
def tax_categories_endpoint():
    return {"categories": TAX_CATEGORIES, "training_examples": training_stats()}

# ---------------------------------------------------------------------------
# Human-in-the-loop review queue
# ---------------------------------------------------------------------------

@app.get("/review-queue")
def review_queue_endpoint():
    queue = df("""
        SELECT t.id, t.created_at, a.name AS agent, t.merchant, t.category,
               t.amount, t.reason, t.risk_score, t.decision_reason
        FROM transactions t JOIN agents a ON t.agent_id = a.id
        WHERE t.status = 'review'
        ORDER BY t.id
    """)
    return queue.to_dict(orient="records")

@app.post("/review-queue/{tx_id}/resolve")
def resolve_review_endpoint(tx_id: int, payload: ReviewResolveRequest, _key=Depends(require_scope("write"))):
    try:
        return resolve_review(tx_id, payload.approve, payload.note)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

# ---------------------------------------------------------------------------
# API Key management (admin scope)
# ---------------------------------------------------------------------------

@app.post("/auth/keys")
def create_api_key(payload: IssueApiKeyRequest, _key=Depends(require_scope("admin"))):
    return issue_api_key(payload.name, payload.scopes)

@app.get("/auth/keys")
def get_api_keys(_key=Depends(require_scope("admin"))):
    return list_api_keys()

@app.post("/auth/keys/{key_id}/rotate")
def rotate_key(key_id: str, _key=Depends(require_scope("admin"))):
    try:
        return rotate_api_key(key_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

@app.delete("/auth/keys/{key_id}")
def revoke_key(key_id: str, _key=Depends(require_scope("admin"))):
    return revoke_api_key(key_id)

# ---------------------------------------------------------------------------
# Solana USDC Payment Rail
# ---------------------------------------------------------------------------

@app.get("/solana/treasury")
def solana_treasury(_key=Depends(require_scope("read"))):
    return treasury_info()

@app.post("/solana/settle/{transaction_id}")
def solana_settle(transaction_id: int, payload: SettleRequest,
                  _key=Depends(require_scope("write"))):
    """
    Settle a Guard-approved transaction on Solana devnet as a USDC transfer.
    In simulation mode (default): builds + signs the transaction locally,
    returns a SIM_ signature as proof of valid key ownership and instruction.
    In live mode (SOLANA_SIMULATE=false): submits to devnet RPC.
    """
    from storage import fetchone as _fetchone
    tx = _fetchone("SELECT * FROM transactions WHERE id = ?", (transaction_id,))
    if not tx:
        raise HTTPException(status_code=404, detail="Transaction not found.")
    if tx["status"] != "approved":
        raise HTTPException(status_code=400,
                            detail=f"Transaction #{transaction_id} is '{tx['status']}', not approved.")
    return settle_payment(
        transaction_id=transaction_id,
        agent_id=tx["agent_id"],
        amount_usd=float(tx["amount"]),
        payee_pubkey_str=payload.payee_pubkey,
        network=payload.network,
    )

@app.get("/solana/payments")
def solana_payments(agent_name: Optional[str] = None, _key=Depends(require_scope("read"))):
    agent_id = None
    if agent_name:
        agent = get_agent_by_name(agent_name)
        if not agent:
            raise HTTPException(status_code=404, detail=f"Agent '{agent_name}' not found.")
        agent_id = agent["id"]
    return list_payments(agent_id)

# ---------------------------------------------------------------------------
# Demo controls
# ---------------------------------------------------------------------------

@app.post("/reset-demo-data")
def reset_demo_data_endpoint(_key=Depends(require_scope("admin"))):
    reset_demo_data()
    invalidate_model()
    return {"status": "reset_complete"}
