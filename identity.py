"""
AI Wallet Guard v5 — Digital Identity for AI Agents (DeID)

Three capabilities, all simulated but with real cryptographic mechanics:

1. DID registration (鏈上身分證 mock)
   Each agent gets a `did:awg:<hash>` identifier and a signing secret.
   DEMO ONLY: the secret is stored server-side so the simulator can sign.
   In production the agent holds the private key; the server stores only
   a public key and verifies asymmetric signatures (e.g. Ed25519).

2. Request signing
   Payment requests can carry an HMAC-SHA256 signature over a canonical
   payload. If an agent has `require_signature` enabled, ANY unsigned or
   badly-signed request is hard-blocked — a hijacked process that knows
   the agent's name but not its key cannot spend.

3. ZK-style owner verification (零知識式擁有者驗證)
   At registration the owner submits only a commitment:
       commitment = sha256(owner_entity || nonce)
   The owner's real identity is never stored. Later, the owner can prove
   control by revealing (entity, nonce) once; the server checks the hash
   and upgrades verification_level to 'zk_verified' — still storing only
   the commitment. This simulates the *flow* of a ZK proof (commit → prove
   → verify). A production version would use a real ZK proof system
   (e.g. Groth16 / Semaphore) so the entity is never revealed even once.

Verified identity feeds the Machine Credit Score (+75 for zk_verified).
"""

import hashlib
import hmac
import json
import secrets
from typing import Optional, Dict, Any

from storage import query_df, execute, now_iso, get_agent, create_alert


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def make_commitment(owner_entity: str, nonce: str) -> str:
    return _sha256(f"{owner_entity.strip().lower()}||{nonce}")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_identity(agent_id: int, owner_entity: str, require_signature: bool = False) -> Dict[str, Any]:
    """
    Issue a DID for the agent. Returns the agent_secret and owner_nonce ONCE —
    the caller must save them (we keep the secret server-side only because this
    is a single-process demo).
    """
    agent = get_agent(agent_id)
    if not agent:
        raise ValueError("Agent not found")
    if get_identity(agent_id):
        raise ValueError(f"Agent '{agent['name']}' already has an identity.")

    agent_secret = secrets.token_hex(32)
    owner_nonce = secrets.token_hex(16)
    commitment = make_commitment(owner_entity, owner_nonce)
    did = f"did:awg:{_sha256(agent['name'] + agent_secret)[:24]}"

    execute("""
        INSERT INTO identities (agent_id, did, agent_secret, owner_commitment,
                                verification_level, require_signature, created_at)
        VALUES (?, ?, ?, ?, 'basic', ?, ?)
    """, (agent_id, did, agent_secret, commitment, int(require_signature), now_iso()))

    create_alert(agent_id, "info",
                 f"IDENTITY ISSUED: {did} (owner identity stored only as commitment; "
                 f"signature requirement: {'ON' if require_signature else 'off'}).")

    return {
        "agent": agent["name"],
        "did": did,
        "agent_secret": agent_secret,      # show once
        "owner_nonce": owner_nonce,        # show once; needed later for the ZK-style proof
        "owner_commitment": commitment,
        "verification_level": "basic",
        "require_signature": require_signature,
    }


def get_identity(agent_id: int) -> Optional[Dict[str, Any]]:
    rows = query_df("SELECT * FROM identities WHERE agent_id = ?", (agent_id,))
    return rows.iloc[0].to_dict() if not rows.empty else None


def set_require_signature(agent_id: int, required: bool):
    execute("UPDATE identities SET require_signature = ? WHERE agent_id = ?",
            (int(required), agent_id))


# ---------------------------------------------------------------------------
# Request signing / verification
# ---------------------------------------------------------------------------

def canonical_payload(agent_name: str, amount: float, merchant: str, category: str, reason: str) -> str:
    return json.dumps({
        "agent_name": agent_name,
        "amount": round(float(amount), 6),
        "merchant": merchant,
        "category": category,
        "reason": reason,
    }, sort_keys=True, separators=(",", ":"))


def sign_payment(agent_secret: str, agent_name: str, amount: float,
                 merchant: str, category: str, reason: str) -> str:
    payload = canonical_payload(agent_name, amount, merchant, category, reason)
    return hmac.new(agent_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def verify_signature(agent_id: int, agent_name: str, amount: float, merchant: str,
                     category: str, reason: str, signature: Optional[str]) -> Dict[str, Any]:
    """
    Returns {"ok": bool, "required": bool, "detail": str}.
    - No identity registered            → ok (signatures not in play yet)
    - Identity, signature optional      → verify if provided, else ok
    - Identity, require_signature = ON  → missing/invalid signature fails
    """
    ident = get_identity(agent_id)
    if not ident:
        return {"ok": True, "required": False, "detail": "No identity registered; signature not enforced."}

    required = bool(ident["require_signature"])
    if not signature:
        if required:
            return {"ok": False, "required": True,
                    "detail": f"Agent {ident['did']} requires signed requests; none provided."}
        return {"ok": True, "required": False, "detail": "Signature optional and not provided."}

    expected = sign_payment(ident["agent_secret"], agent_name, amount, merchant, category, reason)
    if hmac.compare_digest(expected, signature):
        return {"ok": True, "required": required, "detail": f"Valid signature from {ident['did']}."}
    return {"ok": False, "required": required,
            "detail": f"INVALID signature for {ident['did']} — possible impersonation."}


# ---------------------------------------------------------------------------
# ZK-style owner verification
# ---------------------------------------------------------------------------

def verify_owner(agent_id: int, owner_entity: str, owner_nonce: str) -> Dict[str, Any]:
    """Commit-and-reveal proof of ownership. Upgrades the identity to zk_verified."""
    ident = get_identity(agent_id)
    if not ident:
        raise ValueError("Agent has no registered identity.")

    if make_commitment(owner_entity, owner_nonce) == ident["owner_commitment"]:
        execute("UPDATE identities SET verification_level = 'zk_verified' WHERE agent_id = ?", (agent_id,))
        create_alert(agent_id, "info",
                     f"OWNER VERIFIED: {ident['did']} upgraded to zk_verified "
                     f"(commitment matched; owner identity remains unstored).")
        return {"verified": True, "did": ident["did"], "verification_level": "zk_verified"}
    return {"verified": False, "did": ident["did"],
            "detail": "Commitment mismatch. Proof rejected."}


def list_identities():
    return query_df("""
        SELECT i.did, a.name AS agent, i.verification_level, i.require_signature,
               substr(i.owner_commitment, 1, 16) || '…' AS owner_commitment_prefix,
               i.created_at
        FROM identities i JOIN agents a ON i.agent_id = a.id
        ORDER BY i.id
    """)
