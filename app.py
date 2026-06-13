import sqlite3
import json

import pandas as pd
import streamlit as st
import requests

from db import query_df
from solana_rail import treasury_info, list_payments, TREASURY_PUBKEY
from auth import list_api_keys, issue_api_key, rotate_api_key, revoke_api_key
from storage import (
    seed_data,
    reset_demo_data,
    df,
    create_agent,
    get_agent,
    get_wallet,
    credit_wallet,
    update_agent_policy,
    create_alert,
    authorize_payment,
    sweep_idle_funds,
    withdraw_yield,
    accrue_yield,
    resolve_review,
    YIELD_PROTOCOLS,
)
from fintech import compute_credit_score, generate_tax_report, JURISDICTIONS
from identity import register_identity, get_identity, verify_owner, set_require_signature, list_identities
from tax_ai import classify as tax_classify, add_human_label, training_stats, TAX_CATEGORIES

GUARD_API_ROOT = "http://127.0.0.1:8000"

st.set_page_config(page_title="AI Wallet Guard MVP v5", page_icon="🛡️", layout="wide")
seed_data()

st.title("🛡️ AI Wallet Guard MVP v6")
st.caption("AI 支出動態調度與風控平台 — PostgreSQL · API 金鑰認證 · Solana USDC 清算 · Guard API · SDK · DeID · 串流支付 · 信用分 · AI 稅務 · 自動理財")

with st.sidebar:
    st.header("Create Agent (簽發虛擬子卡)")
    with st.form("create_agent"):
        name = st.text_input("Agent name", placeholder="Research Agent")
        purpose = st.text_area("Purpose", placeholder="Buys paid API data for research tasks")
        daily_budget = st.number_input("Daily budget ($)", min_value=0.01, value=10.0, step=1.0)
        per_tx_limit = st.number_input("Per-transaction limit ($)", min_value=0.001, value=1.0, step=0.1, format="%.4f")
        per_minute_spend_limit = st.number_input("60-second spend limit ($)", min_value=0.001, value=3.0, step=0.1, format="%.4f")
        max_txs_per_minute = st.number_input("Max txs per 60 seconds", min_value=1, value=10, step=1)
        submitted = st.form_submit_button("Create + issue wallet")
        if submitted:
            if not name.strip():
                st.error("Agent name is required.")
            else:
                try:
                    create_agent(
                        name=name, purpose=purpose, daily_budget=daily_budget,
                        per_tx_limit=per_tx_limit, per_minute_spend_limit=per_minute_spend_limit,
                        max_txs_per_minute=max_txs_per_minute,
                    )
                    st.success("Agent created with a funded virtual sub-wallet.")
                    st.rerun()
                except sqlite3.IntegrityError:
                    st.error("Agent name already exists.")

    st.divider()
    st.header("Demo Controls")
    if st.button("Reset demo data", type="secondary"):
        reset_demo_data()
        st.success("Demo data reset.")
        st.rerun()

agents = query_df("SELECT * FROM agents ORDER BY id")
agent_options = {f"{row['name']} — {row['status']}": int(row["id"]) for _, row in agents.iterrows()}

tabs = st.tabs([
    "📊 Dashboard",
    "✅ Review Queue",
    "🆔 Identity (DeID)",
    "💳 Wallets & Streaming",
    "🪪 Credit Scores",
    "🧾 Tax & Compliance",
    "🏦 Yield Vault",
    "🔑 API Keys",
    "⛓️ Solana Rail",
    "🧪 Manual Payment",
    "🤖 AI Integration",
    "⚙️ Policies",
    "🚨 Alert Mockup",
    "📁 Audit Export",
])
(tab_dash, tab_review, tab_identity, tab_wallet, tab_credit, tab_tax, tab_yield,
 tab_keys, tab_solana, tab_manual, tab_ai, tab_policy, tab_alert, tab_audit) = tabs

# ---------------------------------------------------------------------------
with tab_dash:
    st.subheader("Agent Overview")
    overview = query_df("""
        SELECT
            a.id, a.name, a.status, a.daily_budget,
            ROUND(w.balance, 4) AS wallet_balance,
            COALESCE(SUM(CASE WHEN t.status='approved' THEN t.amount ELSE 0 END), 0) AS approved_spend,
            COUNT(t.id) AS total_txs,
            SUM(CASE WHEN t.status='blocked' THEN 1 ELSE 0 END) AS blocked_txs,
            SUM(CASE WHEN t.status='review' THEN 1 ELSE 0 END) AS review_txs
        FROM agents a
        LEFT JOIN wallets w ON a.id = w.agent_id
        LEFT JOIN transactions t ON a.id = t.agent_id
        GROUP BY a.id
        ORDER BY a.id
    """)
    if not overview.empty:
        overview["budget_used_%"] = (overview["approved_spend"] / overview["daily_budget"] * 100).round(1)
    st.dataframe(overview, use_container_width=True, hide_index=True)

    tx_stats = query_df("""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status='approved' THEN 1 ELSE 0 END) AS approved,
            SUM(CASE WHEN status='review' THEN 1 ELSE 0 END) AS review,
            SUM(CASE WHEN status='blocked' THEN 1 ELSE 0 END) AS blocked,
            COALESCE(SUM(CASE WHEN status='approved' THEN amount ELSE 0 END), 0) AS approved_amount
        FROM transactions
    """).iloc[0]

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Total transactions", int(tx_stats["total"] or 0))
    c2.metric("Approved", int(tx_stats["approved"] or 0))
    c3.metric("Needs review", int(tx_stats["review"] or 0))
    c4.metric("Blocked", int(tx_stats["blocked"] or 0))
    c5.metric("Approved spend", f"${float(tx_stats['approved_amount'] or 0):.2f}")

    st.subheader("Spend & Risk Charts")
    col_chart1, col_chart2, col_chart3 = st.columns(3)

    with col_chart1:
        spend_by_agent = query_df("""
            SELECT a.name AS agent, COALESCE(SUM(CASE WHEN t.status='approved' THEN t.amount ELSE 0 END), 0) AS approved_spend
            FROM agents a LEFT JOIN transactions t ON a.id = t.agent_id
            GROUP BY a.id ORDER BY a.id
        """)
        if spend_by_agent["approved_spend"].sum() > 0:
            st.bar_chart(spend_by_agent.set_index("agent"))
        else:
            st.info("No approved spend yet.")

    with col_chart2:
        status_counts = query_df("SELECT status, COUNT(*) AS count FROM transactions GROUP BY status ORDER BY status")
        if not status_counts.empty:
            st.bar_chart(status_counts.set_index("status"))
        else:
            st.info("No transactions yet.")

    with col_chart3:
        source_counts = query_df("SELECT source, COUNT(*) AS count FROM transactions GROUP BY source ORDER BY source")
        if not source_counts.empty:
            st.bar_chart(source_counts.set_index("source"))
        else:
            st.info("No API/manual activity yet.")

    st.subheader("Recent Alerts")
    alerts = query_df("""
        SELECT al.created_at, a.name AS agent, al.severity, al.message
        FROM alerts al JOIN agents a ON al.agent_id = a.id
        ORDER BY al.id DESC LIMIT 12
    """)
    st.dataframe(alerts, use_container_width=True, hide_index=True)

    st.subheader("Recent Transactions")
    recent = query_df("""
        SELECT t.created_at, a.name AS agent, t.source, t.merchant, t.category, t.amount, t.status, t.risk_score, t.decision_reason
        FROM transactions t JOIN agents a ON t.agent_id = a.id
        ORDER BY t.id DESC LIMIT 30
    """)
    st.dataframe(recent, use_container_width=True, hide_index=True)

# ---------------------------------------------------------------------------
with tab_review:
    st.subheader("✅ Human-in-the-Loop Review Queue")
    st.write("交易風險分 40–69 會進入審核佇列：人類批准後才扣款，拒絕則封鎖。")

    queue = query_df("""
        SELECT t.id, t.created_at, a.name AS agent, t.merchant, t.category,
               t.amount, t.reason, t.risk_score, t.decision_reason
        FROM transactions t JOIN agents a ON t.agent_id = a.id
        WHERE t.status = 'review'
        ORDER BY t.id
    """)
    if queue.empty:
        st.success("Queue is empty — nothing waiting for human review. "
                   "Run `python agent_simulator.py anomaly` to create one.")
    else:
        for _, tx in queue.iterrows():
            with st.container(border=True):
                c1, c2 = st.columns([3, 1])
                with c1:
                    st.markdown(f"**#{int(tx['id'])} — {tx['agent']} → {tx['merchant']}** "
                                f"&nbsp; ${tx['amount']:.4f} &nbsp; risk {int(tx['risk_score'])}/100")
                    st.caption(f"{tx['decision_reason']}")
                    st.caption(f"Agent memo: {tx['reason']}")
                with c2:
                    if st.button("Approve", key=f"approve_{tx['id']}", type="primary"):
                        resolve_review(int(tx["id"]), True, "Approved via dashboard.")
                        st.rerun()
                    if st.button("Reject", key=f"reject_{tx['id']}"):
                        resolve_review(int(tx["id"]), False, "Rejected via dashboard.")
                        st.rerun()

# ---------------------------------------------------------------------------
with tab_identity:
    st.subheader("🆔 Agent Digital Identity (DeID / 鏈上身分證)")
    st.write("為 Agent 簽發 DID、要求請求簽名、並以 ZK 式承諾驗證擁有者身分（不儲存擁有者真名）。")

    idents = list_identities()
    if idents.empty:
        st.info("No identities issued yet.")
    else:
        st.dataframe(idents, use_container_width=True, hide_index=True)

    col_reg, col_verify = st.columns(2)

    with col_reg:
        st.markdown("### Register identity")
        if agent_options:
            reg_agent = st.selectbox("Agent", list(agent_options.keys()), key="reg_agent")
            owner = st.text_input("Owner entity (never stored — only its commitment)", value="Acme Robotics Inc.")
            req_sig = st.checkbox("Require signed payment requests", value=True)
            if st.button("Issue DID"):
                try:
                    ident = register_identity(agent_options[reg_agent], owner, req_sig)
                    st.success(f"DID issued: {ident['did']}")
                    st.warning("Save these — shown ONCE (demo keeps the secret server-side):")
                    st.code(json.dumps({"agent_secret": ident["agent_secret"],
                                        "owner_nonce": ident["owner_nonce"]}, indent=2), language="json")
                except ValueError as exc:
                    st.error(str(exc))

    with col_verify:
        st.markdown("### ZK-style owner verification")
        if agent_options:
            ver_agent = st.selectbox("Agent ", list(agent_options.keys()), key="ver_agent")
            ver_entity = st.text_input("Owner entity")
            ver_nonce = st.text_input("Owner nonce (from registration)")
            if st.button("Verify owner"):
                try:
                    result = verify_owner(agent_options[ver_agent], ver_entity, ver_nonce)
                    if result["verified"]:
                        st.success(f"{result['did']} upgraded to zk_verified — credit score bonus applied.")
                    else:
                        st.error(result.get("detail", "Proof rejected."))
                except ValueError as exc:
                    st.error(str(exc))

    st.caption("Production roadmap: agent-held Ed25519 keys (server stores only public keys) and a real ZK "
               "proof system (e.g. Semaphore) so the owner is never revealed even at proof time.")


# ---------------------------------------------------------------------------
with tab_wallet:
    st.subheader("💳 Virtual Sub-Wallets (虛擬子卡)")
    wallets = query_df("""
        SELECT a.name AS agent, a.status, ROUND(w.balance, 4) AS balance,
               ROUND(w.total_earned, 4) AS total_earned, ROUND(w.total_spent, 4) AS total_spent,
               w.updated_at
        FROM wallets w JOIN agents a ON w.agent_id = a.id
        ORDER BY a.id
    """)
    st.dataframe(wallets, use_container_width=True, hide_index=True)

    col_earn, col_stream = st.columns(2)
    with col_earn:
        st.markdown("### Simulate agent revenue (機器收入)")
        if agent_options:
            earn_agent = st.selectbox("Agent", list(agent_options.keys()), key="earn_agent")
            earn_amount = st.number_input("Amount earned ($)", min_value=0.0001, value=0.01, step=0.01, format="%.4f")
            if st.button("Credit wallet"):
                credit_wallet(agent_options[earn_agent], earn_amount)
                st.success(f"Credited ${earn_amount:.4f}.")
                st.rerun()

    with col_stream:
        st.markdown("### Streaming payments (串流支付)")
        st.write("Pay-as-you-go streams are driven through the Guard API:")
        st.code("python agent_simulator.py streaming", language="bash")

    st.subheader("Streams (Pay-As-You-Go sessions)")
    streams = query_df("""
        SELECT s.id, a.name AS agent, s.provider, s.unit_type, s.unit_price,
               ROUND(s.units_consumed, 2) AS units, ROUND(s.total_cost, 4) AS total_cost,
               s.status, s.close_reason, s.started_at, s.ended_at
        FROM streams s JOIN agents a ON s.agent_id = a.id
        ORDER BY s.id DESC
    """)
    if streams.empty:
        st.info("No streams yet. Run: python agent_simulator.py streaming")
    else:
        st.dataframe(streams, use_container_width=True, hide_index=True)

# ---------------------------------------------------------------------------
with tab_credit:
    st.subheader("🪪 Machine Credit Scores (機器信用分)")
    st.write("AI 界的 Experian：根據交易歷史、封鎖率、帳齡與收入行為計算 0–1000 信用分。"
             "高信用分的 Agent 可獲得更高的有效單筆額度。")

    if agents.empty:
        st.info("No agents yet.")
    else:
        rows = []
        details = {}
        for _, a in agents.iterrows():
            c = compute_credit_score(int(a["id"]))
            rows.append({
                "agent": c["agent"], "score": c["score"], "tier": c["tier"],
                "limit_multiplier": c["limit_multiplier"],
                "effective_per_tx_limit": c["effective_per_tx_limit"],
                "perk": c["perk"],
            })
            details[c["agent"]] = c["factors"]
        score_df = pd.DataFrame(rows)
        st.dataframe(score_df, use_container_width=True, hide_index=True)
        st.bar_chart(score_df.set_index("agent")["score"])

        picked = st.selectbox("Score breakdown for:", list(details.keys()))
        for f in details[picked]:
            st.write(f"- {f}")

    st.caption("Roadmap: bind agent identity with cryptographic attestation (ZK-proof of the owning entity) "
               "so counterparties can verify a score without exposing the owner.")

# ---------------------------------------------------------------------------
with tab_tax:
    st.subheader("🧾 Real-Time Tax & Compliance Engine — with self-learning AI classifier")
    st.write("自動將機器速度的微支付分類、貼標籤，並依司法管轄區估算稅務影響。"
             "v5 起由 **AI 分類器** 主導：信心不足的項目進入人工覆核，人工標註會即時重訓模型，越用越準。")

    stats = training_stats()
    sc1, sc2, sc3 = st.columns(3)
    sc1.metric("Seed training examples", stats["seed"])
    sc2.metric("Human-taught examples", stats["human"])
    sc3.metric("Human label weight", "3×")

    with st.expander("🧠 AI classifier playground — teach the model", expanded=False):
        pc1, pc2 = st.columns(2)
        with pc1:
            p_merchant = st.text_input("Merchant", value="QuantumLeap-GPU", key="tax_m")
            p_category = st.text_input("Raw category", value="compute", key="tax_c")
            p_reason = st.text_input("Reason / memo", value="Fine-tune LLM on rented H100 cluster", key="tax_r")
            if st.button("Classify"):
                st.session_state["tax_pred"] = tax_classify(p_merchant, p_category, p_reason)
        with pc2:
            pred = st.session_state.get("tax_pred")
            if pred:
                if pred["needs_review"]:
                    st.warning(f"**{pred['label']}** — confidence {pred['confidence']:.2f} → needs human review")
                else:
                    st.success(f"**{pred['label']}** — confidence {pred['confidence']:.2f}")
                correct_label = st.selectbox("Correct label (teach the model):", TAX_CATEGORIES,
                                             index=TAX_CATEGORIES.index(pred["label"]) if pred["label"] in TAX_CATEGORIES else 0)
                if st.button("Confirm & retrain"):
                    add_human_label(p_merchant, p_category, p_reason, correct_label)
                    st.session_state["tax_pred"] = tax_classify(p_merchant, p_category, p_reason)
                    st.success("Model retrained with your label (3× weight). New prediction above.")
                    st.rerun()

    jurisdiction = st.selectbox("Jurisdiction", list(JURISDICTIONS.keys()),
                                format_func=lambda k: JURISDICTIONS[k]["label"], index=2)
    use_ai = st.toggle("Use AI classifier (off = static rules)", value=True)
    report = generate_tax_report(jurisdiction, use_ai=use_ai)

    c1, c2, c3 = st.columns(3)
    c1.metric("Total approved spend", f"${report['total_spend']:.4f}")
    c2.metric("Estimated tax", f"${report['total_estimated_tax']:.4f}")
    c3.metric("Items needing manual review", report.get("review_count", 0))
    st.info(report["note"])

    if isinstance(report["summary"], pd.DataFrame) and not report["summary"].empty:
        st.markdown("### Summary by tax category")
        st.dataframe(report["summary"], use_container_width=True, hide_index=True)

        st.markdown("### Classified line items")
        st.dataframe(report["line_items"], use_container_width=True, hide_index=True)

        csv = report["line_items"].to_csv(index=False).encode("utf-8")
        st.download_button("Download classified tax CSV", data=csv,
                           file_name=f"ai_wallet_guard_tax_{jurisdiction}.csv", mime="text/csv")
    else:
        st.info("No approved transactions yet.")

    st.caption("Demo estimates only. Not tax, legal, or accounting advice.")

# ---------------------------------------------------------------------------
with tab_yield:
    st.subheader("🏦 Yield Vault (自動理財帳戶)")
    st.write("閒置資金（高於每日預算的營運準備金）可自動歸集至模擬的 DeFi / RWA 協議賺取利息。"
             "Agent 被熔斷凍結時，剩餘資金也會自動掃入收益部位。")

    proto_df = pd.DataFrame([
        {"protocol": k, "type": v["type"], "APY": f"{v['apy']*100:.1f}%"}
        for k, v in YIELD_PROTOCOLS.items()
    ])
    st.dataframe(proto_df, use_container_width=True, hide_index=True)

    if agent_options:
        col_a, col_b = st.columns(2)
        with col_a:
            sweep_agent = st.selectbox("Agent", list(agent_options.keys()), key="sweep_agent")
            protocol_choice = st.selectbox("Protocol", ["Auto (best APY)"] + list(YIELD_PROTOCOLS.keys()))
            if st.button("Sweep idle funds"):
                proto = None if protocol_choice.startswith("Auto") else protocol_choice
                result = sweep_idle_funds(agent_options[sweep_agent], protocol=proto, reason="dashboard_sweep")
                if result:
                    st.success(f"Swept ${result['amount']:.4f} into {result['protocol']} at {result['apy']*100:.1f}% APY.")
                else:
                    st.info("No idle funds above the operating reserve (daily budget).")
                st.rerun()
        with col_b:
            withdraw_agent = st.selectbox("Agent ", list(agent_options.keys()), key="withdraw_agent")
            if st.button("Withdraw all positions"):
                result = withdraw_yield(agent_options[withdraw_agent])
                st.success(f"Withdrawn ${result['principal']:.4f} + ${result['interest']:.6f} interest.")
                st.rerun()

    accrue_yield()
    positions = query_df("""
        SELECT y.id, a.name AS agent, y.protocol, ROUND(y.principal, 4) AS principal,
               ROUND(y.apy * 100, 2) AS apy_pct, ROUND(y.accrued_interest, 6) AS accrued_interest,
               y.status, y.created_at, y.withdrawn_at
        FROM yield_positions y JOIN agents a ON y.agent_id = a.id
        ORDER BY y.id DESC
    """)
    st.subheader("Positions")
    if positions.empty:
        st.info("No yield positions yet. Run: python agent_simulator.py earn_and_invest")
    else:
        st.dataframe(positions, use_container_width=True, hide_index=True)
        st.caption("Interest accrual is time-accelerated (×10,000) so demos show visible yield. Refresh to accrue.")

# ---------------------------------------------------------------------------
with tab_keys:
    st.subheader("🔑 API Key Management")
    st.write("每個呼叫都必須帶 `Authorization: Bearer <token>` 或 `X-API-Key: <token>`。"
             "金鑰以 sha256 雜湊儲存，原始 secret 只在簽發時顯示一次。")

    keys_df = list_api_keys()
    if keys_df:
        import pandas as pd
        kdf = pd.DataFrame(keys_df)
        st.dataframe(kdf[["key_id","name","scopes","is_active","created_at","last_used_at"]],
                     use_container_width=True, hide_index=True)
    else:
        st.info("No API keys yet. The root key is bootstrapped on first API startup.")

    col_issue, col_revoke = st.columns(2)
    with col_issue:
        st.markdown("### Issue new key")
        key_name = st.text_input("Key name", value="prod-key", key="kname")
        key_scopes = st.selectbox("Scopes", ["read,write", "read", "read,write,admin"])
        if st.button("Issue key", type="primary"):
            result = issue_api_key(key_name, key_scopes)
            st.success(f"Key issued: **{result['key_id']}**")
            st.warning("⚠️ Copy this token now — shown only once:")
            st.code(result["token"])
            st.rerun()

    with col_revoke:
        st.markdown("### Rotate / revoke")
        if keys_df:
            active_keys = [k for k in keys_df if k["is_active"]]
            if active_keys:
                sel_key = st.selectbox("Select key",
                    options=[k["key_id"] for k in active_keys],
                    format_func=lambda kid: next(f"{k['key_id'][:16]}… ({k['name']})" for k in active_keys if k["key_id"] == kid))
                col_r1, col_r2 = st.columns(2)
                with col_r1:
                    if st.button("🔄 Rotate"):
                        result = rotate_api_key(sel_key)
                        st.success(f"Rotated → new key: {result['key_id']}")
                        st.code(result["token"])
                        st.rerun()
                with col_r2:
                    if st.button("🗑️ Revoke", type="secondary"):
                        revoke_api_key(sel_key)
                        st.success("Key revoked.")
                        st.rerun()

    st.caption("Scopes: `read` = GET endpoints, `write` = payments + mutations, `admin` = key management + reset.")

# ---------------------------------------------------------------------------
with tab_solana:
    st.subheader("⛓️ Solana USDC Payment Rail")
    st.write("Guard 核准的支付可一鍵在 Solana devnet 上以 USDC 清算。"
             "模擬模式（預設）在本地建立並簽署完整的 SPL Token 交易——"
             "Ed25519 簽名證明金鑰擁有權與指令有效性，無需網路存取。"
             "設定 `SOLANA_SIMULATE=false` 並確保 devnet RPC 可達即可切換為真實交易。")

    ti = treasury_info()
    tc1, tc2, tc3, tc4 = st.columns(4)
    tc1.metric("Treasury", ti["treasury_pubkey"][:12] + "…")
    tc2.metric("SOL balance", f"{ti['sol_balance']:.4f}" if ti["sol_balance"] >= 0 else "N/A (RPC blocked)")
    tc3.metric("USDC balance", f"{ti['usdc_balance']:.4f}")
    tc4.metric("Mode", "Simulation ✓" if ti["simulate_mode"] else "⚡ LIVE devnet")

    st.markdown(f"[View treasury on explorer]({ti['explorer']}) &nbsp; | &nbsp; USDC mint: `{ti['usdc_mint']}`")

    st.markdown("### Settle a payment on-chain")
    if agent_options:
        sol_agent = st.selectbox("Agent", list(agent_options.keys()), key="sol_agent")
        agent_id = agent_options[sol_agent]

        pending = query_df("""
            SELECT t.id, t.merchant, t.amount, t.created_at
            FROM transactions t
            LEFT JOIN solana_payments sp ON sp.transaction_id = t.id
            WHERE t.agent_id = ? AND t.status = 'approved' AND sp.id IS NULL
            ORDER BY t.id DESC LIMIT 20
        """, (agent_id,))

        if pending.empty:
            st.info("No unsettled approved transactions. Run a payment first.")
        else:
            st.write(f"**{len(pending)} unsettled approved transactions:**")
            st.dataframe(pending, use_container_width=True, hide_index=True)
            sel_tx = st.selectbox("Transaction to settle", pending["id"].tolist(),
                                  format_func=lambda i: f"#{i} — {pending[pending['id']==i].iloc[0]['merchant']} ${pending[pending['id']==i].iloc[0]['amount']:.4f}")
            payee_pk = st.text_input("Payee Solana pubkey", value=ti["treasury_pubkey"],
                                     help="In production: the payee's actual wallet address")
            if st.button("Settle on Solana ⛓️", type="primary"):
                from storage import fetchone as _fetchone
                tx = _fetchone("SELECT * FROM transactions WHERE id = ?", (int(sel_tx),))
                result = __import__('solana_rail').settle_payment(
                    int(sel_tx), agent_id, float(tx["amount"]), payee_pk)
                if result["status"] == "simulated":
                    st.success(f"✅ Simulated: locally signed & recorded on-chain (simulation mode)")
                    st.code(json.dumps({k: result[k] for k in
                        ["status","tx_signature","simulation_result","tx_bytes_length","from","to","amount_usdc"]}, indent=2))
                elif result["status"] == "confirmed":
                    st.success(f"✅ Confirmed on Solana devnet!")
                    st.markdown(f"[View on Explorer]({result['explorer_url']})")
                else:
                    st.error(f"Failed: {result.get('error')}")
                st.rerun()

    st.subheader("On-chain Payment Records")
    payments = list_payments()
    if not payments:
        st.info("No Solana payments recorded yet.")
    else:
        import pandas as pd
        pf = pd.DataFrame(payments)[["id","agent_id","amount_usdc","status","tx_signature","created_at","confirmed_at"]]
        st.dataframe(pf, use_container_width=True, hide_index=True)

    with st.expander("Going live on devnet", expanded=False):
        st.markdown("""
**To submit real transactions on Solana devnet:**

```bash
export SOLANA_SIMULATE=false
# Restart the Guard API — it will attempt to fund the treasury via devnet airdrop
python -m uvicorn guard_api:app --reload --port 8000
```

The treasury wallet is `""" + ti["treasury_pubkey"] + """`.
Fund it with devnet USDC from the [Solana devnet faucet](https://spl-token-faucet.com/?token-name=USDC-Dev)
or request an airdrop via `GET /solana/treasury` (triggers auto-airdrop if SOL < 0.1).

The devnet USDC mint is `""" + ti["usdc_mint"] + """` (Circle's publicly documented test token).
        """)

# ---------------------------------------------------------------------------
with tab_manual:
    st.subheader("Manual Payment Test")
    st.write("This is for human testing. In real AI integration, the AI Agent uses the Guard API instead.")

    if not agent_options:
        st.warning("Create an agent first.")
    else:
        selected = st.selectbox("Agent", list(agent_options.keys()))
        agent_id = agent_options[selected]
        selected_agent = get_agent(agent_id)
        wallet = get_wallet(agent_id) or {"balance": 0}

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Status", selected_agent["status"])
        c2.metric("Wallet", f"${wallet['balance']:.4f}")
        c3.metric("Daily budget", f"${selected_agent['daily_budget']:.2f}")
        c4.metric("Per-tx limit", f"${selected_agent['per_tx_limit']:.4f}")
        c5.metric("60-sec spend limit", f"${selected_agent['per_minute_spend_limit']:.4f}")

        with st.form("payment"):
            amount = st.number_input("Amount ($)", min_value=0.0001, value=0.05, step=0.01, format="%.4f")
            merchant = st.text_input("Merchant", value="PaidDataAPI")
            category = st.selectbox("Category", ["data api", "compute", "image generation", "database", "software", "stream", "unknown", "crypto", "external transfer"])
            reason = st.text_area("Agent reason / payment memo", value="Need to buy one paid search result for research.")
            pay = st.form_submit_button("Run policy check")
            if pay:
                result = authorize_payment(agent_id, amount, merchant, category, reason, source="manual")
                if result["status"] == "approved":
                    st.success(f"Approved. Risk score: {result['risk_score']}/100. {result['decision_reason']}")
                elif result["status"] == "review":
                    st.warning(f"Needs review. Risk score: {result['risk_score']}/100. {result['decision_reason']}")
                else:
                    st.error(f"Blocked. Risk score: {result['risk_score']}/100. {result['decision_reason']}")
                st.rerun()

# ---------------------------------------------------------------------------
with tab_ai:
    st.subheader("AI Agent Integration")
    st.write("AI agents must call the Guard API before spending money.")

    col1, col2 = st.columns(2)
    with col1:
        st.markdown("### 1. Start Guard API")
        st.code("python -m uvicorn guard_api:app --reload --port 8000", language="bash")
        try:
            health = requests.get(f"{GUARD_API_ROOT}/health", timeout=1).json()
            st.success(f"Guard API is running: {health}")
        except Exception:
            st.error("Guard API is not running yet. Start it in a separate Terminal.")

    with col2:
        st.markdown("### 2. Run AI Agent Simulator")
        for m in ["normal", "loop", "prompt_injection", "large_payment",
                  "anomaly", "streaming", "earn_and_invest", "identity",
                  "tax_learning", "sdk", "credit", "tax", "mixed"]:
            st.code(f"python agent_simulator.py {m}", language="bash")

    st.markdown("### 3. Integrate a REAL agent with the SDK")
    st.code(
        'from wallet_guard_sdk import WalletGuard, SpendingDenied\n\n'
        'guard = WalletGuard(agent_name="Research Agent", agent_secret="...")  # signed\n\n'
        '@guard.protect(amount=0.05, merchant="PaidDataAPI", category="data api",\n'
        '               reason="buy one search result")\n'
        'def buy_search_result():\n'
        '    return call_the_paid_api()   # only runs if the Guard approves\n\n'
        'with guard.stream(provider="GPU-Rent-Node", unit_type="per_second",\n'
        '                  unit_price=0.001) as s:\n'
        '    s.tick(1)  # raises SpendingDenied if the Guard kills the stream',
        language="python")
    st.markdown("LangChain: `tools = [guard.as_langchain_tool()]` &nbsp;·&nbsp; "
                "Claude tool-use: `WalletGuard.anthropic_tool_schema()` — see `sdk_examples.py`")

    st.markdown("### Example AI payment authorization request")
    st.code(json.dumps({
        "agent_name": "Research Agent",
        "amount": 0.05,
        "merchant": "PaidDataAPI",
        "category": "data api",
        "reason": "Need to buy one paid search result for research.",
        "task_id": "task_001"
    }, indent=2), language="json")

    st.markdown("### Example streaming (pay-as-you-go) flow")
    st.code(
        'POST /gateway/start-stream  {"agent_name": "Image Agent", "provider": "GPU-Rent-Node", '
        '"unit_type": "per_second", "unit_price": 0.001}\n'
        'POST /gateway/stream/{id}/tick  {"units": 1}   # guard-checked + debited every tick\n'
        'POST /gateway/stream/{id}/stop',
        language="text")

    st.info("AI Agent can request spending, but AI Wallet Guard decides whether the agent is allowed to spend.")

# ---------------------------------------------------------------------------
with tab_policy:
    st.subheader("Edit Agent Policies")

    if not agent_options:
        st.warning("Create an agent first.")
    else:
        selected_policy = st.selectbox("Choose agent", list(agent_options.keys()), key="policy_agent")
        policy_agent_id = agent_options[selected_policy]
        agent = get_agent(policy_agent_id)

        with st.form("edit_policy"):
            new_name = st.text_input("Agent name", value=agent["name"])
            new_purpose = st.text_area("Purpose", value=agent["purpose"] or "")
            new_daily = st.number_input("Daily budget ($)", min_value=0.01, value=float(agent["daily_budget"]), step=1.0)
            new_per_tx = st.number_input("Per-transaction limit ($)", min_value=0.001, value=float(agent["per_tx_limit"]), step=0.1, format="%.4f")
            new_per_min = st.number_input("60-second spend limit ($)", min_value=0.001, value=float(agent["per_minute_spend_limit"]), step=0.1, format="%.4f")
            new_max_txs = st.number_input("Max txs per 60 seconds", min_value=1, value=int(agent["max_txs_per_minute"]), step=1)
            new_status = st.selectbox("Status", ["active", "frozen"], index=0 if agent["status"] == "active" else 1)

            save_policy = st.form_submit_button("Save policy")
            if save_policy:
                try:
                    update_agent_policy(
                        agent_id=policy_agent_id, name=new_name, purpose=new_purpose,
                        daily_budget=new_daily, per_tx_limit=new_per_tx,
                        per_minute_spend_limit=new_per_min, max_txs_per_minute=new_max_txs,
                        status=new_status,
                    )
                    create_alert(policy_agent_id, "info", "Agent policy updated by admin.")
                    st.success("Policy updated.")
                    st.rerun()
                except sqlite3.IntegrityError:
                    st.error("Agent name already exists.")

        st.subheader("All Policies")
        policies = query_df("""
            SELECT id, name, purpose, daily_budget, per_tx_limit, per_minute_spend_limit, max_txs_per_minute, status
            FROM agents ORDER BY id
        """)
        st.dataframe(policies, use_container_width=True, hide_index=True)

# ---------------------------------------------------------------------------
with tab_alert:
    st.subheader("Slack / Email Alert Mockup")
    latest_alert = query_df("""
        SELECT al.id, al.created_at, a.name AS agent, al.severity, al.message
        FROM alerts al JOIN agents a ON al.agent_id = a.id
        ORDER BY al.id DESC LIMIT 1
    """)

    if latest_alert.empty:
        st.info("No alerts yet. Run a blocked transaction or an AI simulator test first.")
    else:
        row = latest_alert.iloc[0]
        st.write("This is a mock alert payload that could be sent to Slack, email, or PagerDuty.")
        payload = {
            "product": "AI Wallet Guard",
            "severity": row["severity"],
            "agent": row["agent"],
            "message": row["message"],
            "created_at": row["created_at"],
            "recommended_action": "Review transaction, check agent prompt/task history, and unfreeze only if safe.",
        }
        st.code(json.dumps(payload, indent=2), language="json")

        st.markdown("### Example message")
        st.warning(
            f"AI Wallet Guard Alert — {row['severity'].upper()}\n\n"
            f"Agent: {row['agent']}\n\n"
            f"{row['message']}\n\n"
            "Recommended action: review the payment trail before reactivating this agent."
        )

# ---------------------------------------------------------------------------
with tab_audit:
    st.subheader("Audit Log Export")
    audit = query_df("""
        SELECT t.id, t.created_at, a.name AS agent, a.purpose, t.source, t.merchant,
               t.category, t.amount, t.reason, t.status, t.risk_score, t.decision_reason
        FROM transactions t JOIN agents a ON t.agent_id = a.id
        ORDER BY t.id DESC
    """)
    st.dataframe(audit, use_container_width=True, hide_index=True)

    csv = audit.to_csv(index=False).encode("utf-8")
    st.download_button(
        label="Download audit CSV",
        data=csv,
        file_name="ai_wallet_guard_audit_log_v6.csv",
        mime="text/csv",
    )

st.caption("Demo only. This MVP does not move money, provide financial/tax/legal advice, or connect to real bank/card/crypto rails.")
