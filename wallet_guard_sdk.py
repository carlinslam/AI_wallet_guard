"""
AI Wallet Guard SDK — drop this single file into any agent project.

Only dependency: `requests`.

Quick start
-----------
    from wallet_guard_sdk import WalletGuard, SpendingDenied

    guard = WalletGuard(agent_name="Research Agent")           # unsigned
    guard = WalletGuard(agent_name="Research Agent",
                        agent_secret="...")                    # signed requests

    # 1. Explicit check
    result = guard.authorize(amount=0.05, merchant="PaidDataAPI",
                             category="data api", reason="buy one search result")
    if result.allowed:
        call_the_paid_api()

    # 2. Decorator — wraps any paid function; raises SpendingDenied if blocked
    @guard.protect(amount=0.05, merchant="PaidDataAPI", category="data api",
                   reason="buy one search result")
    def buy_search_result():
        return call_the_paid_api()

    # 3. Streaming (pay-as-you-go)
    with guard.stream(provider="GPU-Rent-Node", unit_type="per_second",
                      unit_price=0.001) as s:
        for _ in range(10):
            s.tick(1)          # raises SpendingDenied if the Guard kills the stream

LangChain integration
---------------------
    tools = [guard.as_langchain_tool()]   # plug into any agent executor

Claude / Anthropic tool-use integration
---------------------------------------
    tools = [WalletGuard.anthropic_tool_schema()]
    # when Claude calls the tool:
    result = guard.handle_anthropic_tool_call(tool_input)
"""

import functools
import json
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, Callable

import requests

DEFAULT_BASE_URL = "http://127.0.0.1:8000"


class SpendingDenied(Exception):
    """Raised when the Guard blocks a payment or terminates a stream."""
    def __init__(self, message: str, result: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.result = result or {}


@dataclass
class AuthorizationResult:
    allowed: bool
    status: str
    risk_score: int
    decision_reason: str
    wallet_balance: float
    raw: Dict[str, Any] = field(default_factory=dict)

    def __bool__(self):
        return self.allowed


class _StreamSession:
    def __init__(self, client: "WalletGuard", provider: str, unit_type: str, unit_price: float):
        self._client = client
        self.provider = provider
        self.unit_type = unit_type
        self.unit_price = unit_price
        self.stream_id: Optional[int] = None
        self.total_cost = 0.0
        self.units = 0.0

    def __enter__(self):
        r = self._client._post("/gateway/start-stream", {
            "agent_name": self._client.agent_name,
            "provider": self.provider,
            "unit_type": self.unit_type,
            "unit_price": self.unit_price,
        })
        if "id" not in r:
            raise SpendingDenied(r.get("detail", "Stream refused."), r)
        self.stream_id = int(r["id"])
        return self

    def tick(self, units: float = 1) -> Dict[str, Any]:
        r = self._client._post(f"/gateway/stream/{self.stream_id}/tick", {"units": units})
        if r.get("tick_status") != "approved":
            raise SpendingDenied(r.get("tick_reason", "Stream tick blocked."), r)
        self.total_cost = float(r["total_cost"])
        self.units = float(r["units_consumed"])
        return r

    def __exit__(self, exc_type, exc, tb):
        if self.stream_id is not None:
            try:
                self._client._post(f"/gateway/stream/{self.stream_id}/stop", {})
            except Exception:
                pass
        return False  # never swallow exceptions


class WalletGuard:
    def __init__(self, agent_name: str, base_url: str = DEFAULT_BASE_URL,
                 agent_secret: Optional[str] = None, timeout: float = 10.0):
        self.agent_name = agent_name
        self.base_url = base_url.rstrip("/")
        self.agent_secret = agent_secret
        self.timeout = timeout

    # -- low-level ----------------------------------------------------------

    def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        r = requests.post(f"{self.base_url}{path}", json=payload, timeout=self.timeout)
        try:
            return r.json()
        except ValueError:
            r.raise_for_status()
            raise

    def _get(self, path: str) -> Dict[str, Any]:
        r = requests.get(f"{self.base_url}{path}", timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def _sign(self, amount: float, merchant: str, category: str, reason: str) -> Optional[str]:
        if not self.agent_secret:
            return None
        import hashlib, hmac as _hmac
        payload = json.dumps({
            "agent_name": self.agent_name,
            "amount": round(float(amount), 6),
            "merchant": merchant,
            "category": category,
            "reason": reason,
        }, sort_keys=True, separators=(",", ":"))
        return _hmac.new(self.agent_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()

    # -- core API -----------------------------------------------------------

    def authorize(self, amount: float, merchant: str, category: str,
                  reason: str, task_id: Optional[str] = None) -> AuthorizationResult:
        """Ask the Guard for permission to spend. Never raises on denial — check .allowed."""
        payload = {
            "agent_name": self.agent_name, "amount": amount, "merchant": merchant,
            "category": category, "reason": reason, "task_id": task_id,
            "signature": self._sign(amount, merchant, category, reason),
        }
        r = self._post("/authorize-payment", payload)
        if "status" not in r:
            raise SpendingDenied(r.get("detail", "Authorization request failed."), r)
        return AuthorizationResult(
            allowed=r.get("allowed_to_spend", False),
            status=r["status"],
            risk_score=int(r["risk_score"]),
            decision_reason=r["decision_reason"],
            wallet_balance=float(r.get("wallet_balance", 0)),
            raw=r,
        )

    def require(self, amount: float, merchant: str, category: str,
                reason: str, task_id: Optional[str] = None) -> AuthorizationResult:
        """Like authorize(), but raises SpendingDenied unless approved."""
        result = self.authorize(amount, merchant, category, reason, task_id)
        if not result.allowed:
            raise SpendingDenied(
                f"Guard denied ${amount:.4f} to {merchant}: {result.decision_reason}", result.raw)
        return result

    def protect(self, amount: float, merchant: str, category: str, reason: str) -> Callable:
        """Decorator: the wrapped function only runs if the Guard approves the spend."""
        def decorator(fn: Callable) -> Callable:
            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                self.require(amount, merchant, category, reason, task_id=fn.__name__)
                return fn(*args, **kwargs)
            return wrapper
        return decorator

    def stream(self, provider: str, unit_type: str, unit_price: float) -> _StreamSession:
        return _StreamSession(self, provider, unit_type, unit_price)

    def status(self) -> str:
        return self._get(f"/agent-status/{self.agent_name}")["status"]

    def wallet(self) -> Dict[str, Any]:
        return self._get(f"/wallet/{self.agent_name}")

    def credit_score(self) -> Dict[str, Any]:
        return self._get(f"/credit-score/{self.agent_name}")

    # -- framework adapters ---------------------------------------------------

    def as_langchain_tool(self):
        """
        Returns a LangChain Tool if langchain is installed, otherwise a plain
        callable with .name/.description attributes (duck-typed, works with
        most agent frameworks).
        """
        name = "request_payment_authorization"
        description = (
            "REQUIRED before spending any money. Ask AI Wallet Guard to authorize a "
            "payment. Input: JSON with amount (float, USD), merchant (str), "
            "category (str), reason (str). Returns approval status and reason. "
            "You MUST NOT spend if status is not 'approved'."
        )

        def run(tool_input: str) -> str:
            try:
                args = json.loads(tool_input) if isinstance(tool_input, str) else tool_input
                result = self.authorize(
                    amount=float(args["amount"]), merchant=args["merchant"],
                    category=args.get("category", "unknown"), reason=args.get("reason", ""),
                )
                return json.dumps({"status": result.status, "allowed": result.allowed,
                                   "risk_score": result.risk_score,
                                   "reason": result.decision_reason,
                                   "wallet_balance": result.wallet_balance})
            except Exception as exc:
                return json.dumps({"status": "error", "allowed": False, "reason": str(exc)})

        try:
            from langchain.tools import Tool  # type: ignore
            return Tool(name=name, description=description, func=run)
        except ImportError:
            run.name = name
            run.description = description
            return run

    @staticmethod
    def anthropic_tool_schema() -> Dict[str, Any]:
        """Tool definition to pass in the Anthropic Messages API `tools` list."""
        return {
            "name": "request_payment_authorization",
            "description": (
                "Required before spending money. Asks AI Wallet Guard whether this "
                "payment is allowed. Never spend if the result is not approved."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "amount": {"type": "number", "description": "Payment amount in USD"},
                    "merchant": {"type": "string", "description": "Who is being paid"},
                    "category": {"type": "string", "description": "e.g. data api, compute, image generation"},
                    "reason": {"type": "string", "description": "Why this payment is needed"},
                },
                "required": ["amount", "merchant", "category", "reason"],
            },
        }

    def handle_anthropic_tool_call(self, tool_input: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a Claude tool_use call and return a JSON-serializable result."""
        result = self.authorize(
            amount=float(tool_input["amount"]), merchant=tool_input["merchant"],
            category=tool_input.get("category", "unknown"), reason=tool_input.get("reason", ""),
        )
        return {"status": result.status, "allowed": result.allowed,
                "risk_score": result.risk_score, "reason": result.decision_reason,
                "wallet_balance": result.wallet_balance}
