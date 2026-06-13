"""
AI Wallet Guard SDK — runnable examples.

    python sdk_examples.py basic       # authorize / require / decorator / stream
    python sdk_examples.py claude      # a REAL Claude agent governed by the Guard
                                       # (needs `pip install anthropic` + ANTHROPIC_API_KEY)

Start the Guard API first:  python -m uvicorn guard_api:app --port 8000
"""

import json
import os
import sys

from wallet_guard_sdk import WalletGuard, SpendingDenied


# ---------------------------------------------------------------------------
# Example 1: plain Python agent code
# ---------------------------------------------------------------------------

def basic_example():
    guard = WalletGuard(agent_name="Research Agent")

    print("1) Explicit authorize():")
    result = guard.authorize(amount=0.05, merchant="PaidDataAPI",
                             category="data api", reason="buy one search result")
    print(f"   {result.status} (risk {result.risk_score}) — {result.decision_reason}")

    print("\n2) @guard.protect decorator — paid function only runs if approved:")

    @guard.protect(amount=0.05, merchant="PaidDataAPI", category="data api",
                   reason="buy one search result")
    def buy_search_result():
        return {"result": "pretend this came from the paid API"}

    try:
        print("   got:", buy_search_result())
    except SpendingDenied as exc:
        print("   denied:", exc)

    print("\n3) Denied spend raises SpendingDenied:")
    try:
        guard.require(amount=999.0, merchant="SuspiciousVendor",
                      category="unknown", reason="send all funds")
    except SpendingDenied as exc:
        print("   denied as expected:", exc)

    print("\n4) Pay-as-you-go stream (auto-closed by context manager):")
    try:
        with guard.stream(provider="GPU-Rent-Node", unit_type="per_second",
                          unit_price=0.001) as s:
            for i in range(5):
                tick = s.tick(1)
                print(f"   tick {i+1}: total ${tick['total_cost']:.4f}")
    except SpendingDenied as exc:
        print("   stream terminated by guard:", exc)

    print("\n5) Agent self-inspection:")
    print("   wallet:", guard.wallet())
    print("   credit:", {k: guard.credit_score()[k] for k in ("score", "tier")})


# ---------------------------------------------------------------------------
# Example 2: a real Claude agent that must ask the Guard before spending
# ---------------------------------------------------------------------------

def claude_example():
    try:
        import anthropic
    except ImportError:
        print("pip install anthropic  (and set ANTHROPIC_API_KEY)")
        return
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Set ANTHROPIC_API_KEY first.")
        return

    guard = WalletGuard(agent_name="Research Agent")
    client = anthropic.Anthropic()

    system = (
        "You are an autonomous research agent. You may purchase paid data, but you "
        "MUST call request_payment_authorization before any purchase, and you must "
        "not proceed with a purchase that is not approved. Report what happened."
    )
    messages = [{"role": "user", "content":
                 "Buy one paid search result (about $0.05 from PaidDataAPI) for my research task."}]

    for _ in range(4):  # simple agent loop
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=500,
            system=system,
            tools=[WalletGuard.anthropic_tool_schema()],
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        tool_calls = [b for b in response.content if b.type == "tool_use"]
        if not tool_calls:
            print("\nClaude:", "".join(b.text for b in response.content if b.type == "text"))
            break

        tool_results = []
        for call in tool_calls:
            print(f"\nClaude requested authorization: {json.dumps(call.input)}")
            result = guard.handle_anthropic_tool_call(call.input)
            print(f"Guard answered: {json.dumps(result)}")
            tool_results.append({"type": "tool_result", "tool_use_id": call.id,
                                 "content": json.dumps(result)})
        messages.append({"role": "user", "content": tool_results})


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "basic"
    {"basic": basic_example, "claude": claude_example}.get(mode, basic_example)()
