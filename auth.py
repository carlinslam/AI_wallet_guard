"""
AI Wallet Guard v6 — API Key Authentication

Every request to the Guard API must carry either:
    Authorization: Bearer awg_<key_id>.<raw_secret>
or (legacy / curl-friendly):
    X-API-Key: awg_<key_id>.<raw_secret>

Key lifecycle:
    issue_api_key(name, scopes)  → returns the full key ONCE (store it safely)
    rotate_api_key(key_id)       → revokes old key, issues a new one under the same name
    revoke_api_key(key_id)       → marks the key inactive
    list_api_keys()              → shows all keys (key_id, name, scopes — never the secret)

Scopes (checked per-endpoint):
    read    GET endpoints (status, agents, dashboard queries)
    write   POST endpoints that mutate state (authorize-payment, create agent, …)
    admin   Sensitive operations (reset demo data, issue API keys, etc.)

The root bootstrap key is created automatically on first startup and printed
to stdout / stored in .env so the operator can make their first API call.

Security notes (demo → production gap):
    - Secrets are stored as sha256(secret). Production should use bcrypt/Argon2.
    - Rate limiting per key_id should sit in front (nginx / Kong / Cloudflare).
    - Key expiry (expires_at column) is checked but not yet enforced at rotation time.
"""

import hashlib
import os
import secrets
from datetime import datetime, timezone
from typing import Optional, List

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer, APIKeyHeader

from db import execute, fetchone, fetchall, query_df

_bearer = HTTPBearer(auto_error=False)
_header = APIKeyHeader(name="X-API-Key", auto_error=False)


# ---------------------------------------------------------------------------
# Key storage helpers
# ---------------------------------------------------------------------------

def _hash_secret(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _make_key_id() -> str:
    return "awg_" + secrets.token_urlsafe(12)


def issue_api_key(name: str, scopes: str = "read,write") -> dict:
    """
    Create a new API key. Returns the full token ONCE — we store only the hash.
    Token format: awg_<key_id>.<raw_secret>
    """
    raw_secret = secrets.token_urlsafe(32)
    key_id = _make_key_id()
    key_hash = _hash_secret(raw_secret)
    execute("""
        INSERT INTO api_keys (key_id, key_hash, name, scopes, created_at)
        VALUES (?, ?, ?, ?, ?)
    """, (key_id, key_hash, name, scopes, datetime.now(timezone.utc).isoformat()))
    full_token = f"{key_id}.{raw_secret}"
    return {"key_id": key_id, "token": full_token, "name": name, "scopes": scopes,
            "message": "Store this token securely — the secret is shown only once."}


def rotate_api_key(key_id: str) -> dict:
    """Revoke an existing key and issue a replacement with the same name/scopes."""
    row = fetchone("SELECT * FROM api_keys WHERE key_id = ? AND is_active = TRUE", (key_id,))
    if not row:
        raise ValueError(f"Active key '{key_id}' not found.")
    execute("UPDATE api_keys SET is_active = FALSE WHERE key_id = ?", (key_id,))
    return issue_api_key(name=row["name"], scopes=row["scopes"])


def revoke_api_key(key_id: str) -> dict:
    execute("UPDATE api_keys SET is_active = FALSE WHERE key_id = ?", (key_id,))
    return {"key_id": key_id, "revoked": True}


def list_api_keys() -> list:
    return fetchall("""
        SELECT key_id, name, scopes, is_active, created_at, last_used_at, expires_at
        FROM api_keys ORDER BY created_at DESC
    """)


def _parse_token(token: str) -> Optional[tuple[str, str]]:
    """Split 'awg_<key_id>.<raw_secret>' → (key_id, raw_secret)."""
    if not token or token.count(".") < 1:
        return None
    # key_id itself contains underscores; split on FIRST dot after 'awg_...'
    idx = token.index(".")
    return token[:idx], token[idx + 1:]


def _verify_token(token: str, required_scope: str) -> dict:
    """Verify token, update last_used_at, check scope. Returns the key row."""
    parsed = _parse_token(token)
    if not parsed:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid token format. Expected awg_<key_id>.<secret>")
    key_id, raw_secret = parsed
    row = fetchone("SELECT * FROM api_keys WHERE key_id = ? AND is_active = TRUE", (key_id,))
    if not row:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Unknown or revoked API key.")
    if row.get("expires_at"):
        exp = datetime.fromisoformat(str(row["expires_at"]))
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > exp:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="API key has expired.")
    if _hash_secret(raw_secret) != row["key_hash"]:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Invalid API key secret.")
    if required_scope and required_scope not in (row.get("scopes") or ""):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail=f"Scope '{required_scope}' required; key has '{row['scopes']}'.")
    execute("UPDATE api_keys SET last_used_at = ? WHERE key_id = ?",
            (datetime.now(timezone.utc).isoformat(), key_id))
    return row


# ---------------------------------------------------------------------------
# FastAPI dependency factories
# ---------------------------------------------------------------------------

def _extract_token(
    bearer: Optional[HTTPAuthorizationCredentials] = Security(_bearer),
    header_key: Optional[str] = Security(_header),
) -> Optional[str]:
    if bearer:
        return bearer.credentials
    return header_key


def require_scope(scope: str):
    """
    Returns a FastAPI dependency that verifies the caller has `scope`.
    Usage:
        @app.post("/authorize-payment")
        def ep(payload: ..., _key = Depends(require_scope("write"))):
            ...
    Pass scope="" to just authenticate (any valid key).
    """
    def _dep(token: Optional[str] = Depends(_extract_token)) -> dict:
        if token is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="Missing API key. Pass Authorization: Bearer <token> or X-API-Key: <token>")
        return _verify_token(token, scope)
    return _dep


# ---------------------------------------------------------------------------
# Bootstrap: create the root key if no active keys exist
# ---------------------------------------------------------------------------

def bootstrap_root_key() -> Optional[str]:
    """
    Called at API startup. Creates one 'admin' key if the table is empty,
    prints the token, and persists it in .env so it survives restarts.
    Returns the token string, or None if keys already exist.
    """
    existing = fetchall("SELECT id FROM api_keys WHERE is_active = TRUE LIMIT 1")
    if existing:
        return None

    result = issue_api_key(name="root (auto-generated)", scopes="read,write,admin")
    token = result["token"]

    env_path = os.path.join(os.path.dirname(__file__), ".env")
    # Only update .env + print banner if we don't already have this key stored
    if not _env_has_key(env_path, "AWG_ROOT_API_KEY"):
        _update_env(env_path, "AWG_ROOT_API_KEY", token)
        banner = (
            "\n" + "=" * 70 + "\n"
            "  AI Wallet Guard — ROOT API KEY (shown once, saved to .env)\n\n"
            f"  {token}\n\n"
            "  Set as an env var:  export AWG_ROOT_API_KEY=<token>\n"
            "  Or keep in .env:    AWG_ROOT_API_KEY=<token>\n"
            + "=" * 70 + "\n"
        )
        print(banner)
    return token


def _env_has_key(path: str, key: str) -> bool:
    if not os.path.exists(path):
        return False
    return any(line.startswith(f"{key}=") for line in open(path).readlines())


def _update_env(path: str, key: str, value: str):
    """Write or overwrite a KEY=value line in .env."""
    lines = []
    found = False
    if os.path.exists(path):
        for line in open(path).readlines():
            if line.startswith(f"{key}="):
                lines.append(f"{key}={value}\n")
                found = True
            else:
                lines.append(line)
    if not found:
        lines.append(f"{key}={value}\n")
    with open(path, "w") as f:
        f.writelines(lines)
