# AI Wallet Guard MVP v6

v6 upgrades the project into a truly deployable, enterprise-grade service infrastructure:

| Feature | v5 | v6 |
|---|---|---|
| **Database** | Local SQLite file | **PostgreSQL + SQLAlchemy Connection Pool + Alembic Migrations** |
| **API Security** | Unauthenticated, open to all | **API Key Authentication (Bearer token / X-API-Key, SHA-256 hash storage, Scope enforcement)** |
| **Payment Settlement** | Pure simulation, no on-chain records | **Solana Devnet USDC — Real SPL Token transaction instructions + Ed25519 signatures. Supports both local simulation and live on-chain broadcasting.** |
| **Schema Management** | Manual table creation | Alembic migrations (`alembic upgrade head`) |
| **Key Management** | — | Issue / rotate / revoke + enforced scopes (`read` / `write` / `admin`) |
| **Fallback System** | — | Graceful fallback to SQLite if Postgres is unreachable (zero-config for CI/local dev) |

All v5 features (**DeID Identity, Self-learning Tax Classifier, Agent SDK, Streaming Payments, Machine Credit Scores, Review Queue, Yield Vault**) are fully preserved.

---

## New Architecture Files

- `db.py`: SQLAlchemy engine + connection pool + SQLite fallback + param normalization.
- `auth.py`: API key issuance, verification, rotation, scope control, `bootstrap_root_key()`.
- `solana_rail.py`: Solana devnet USDC settlement: SPL Token `transfer_checked` + Ed25519 signatures.
- `migrations/env.py`: Alembic environment config.
- `migrations/versions/0001_initial_schema_v6.py`: Complete schema + indexes (including `api_keys`, `solana_payments`).
- `alembic.ini`: Alembic configurations.

---

## Installation

```bash
pip install -r requirements.txt

# Database (Required. Or set DATABASE_BACKEND=sqlite to use local SQLite)
DATABASE_URL=postgresql+psycopg2://awg_user:awg_pass@localhost:5432/ai_wallet_guard_v6

# Solana (Optional)
SOLANA_SIMULATE=true          # true = local signature simulation; false = live devnet broadcasting
SOLANA_RPC_URL=[https://api.devnet.solana.com](https://api.devnet.solana.com)

# Slack (Optional)
SLACK_WEBHOOK_URL=[https://hooks.slack.com/services/](https://hooks.slack.com/services/)…   # Auto-push for critical alerts


# Create Postgres user + database (One-time setup)
createuser -P awg_user
createdb -O awg_user ai_wallet_guard_v6

# Run migrations
alembic upgrade head
---

## Quick Start

# Terminal 1 — Guard API (Auto-prints the root API key on first startup)
python -m uvicorn guard_api:app --reload --port 8000

# Terminal 2 — Streamlit Dashboard
streamlit run app.py

# Terminal 3 — AI Agent Simulator (Runs the full 14-act demo story)
python agent_simulator.py mixed
---

## API Authentication
# Using Bearer token
curl -H "Authorization: Bearer awg_xxx.yyy" http://localhost:8000/authorize-payment ...

# Or using X-API-Key header
curl -H "X-API-Key: awg_xxx.yyy" ...

Root key 在 API 第一次啟動時自動產生並印到 stdout，同時寫入 `.env`。

### Key Management Endpoints

POST   /auth/keys                    # Issue a new key (requires 'admin' scope)
GET    /auth/keys                    # List all active keys (requires 'admin' scope)
POST   /auth/keys/{key_id}/rotate    # Rotate → revokes old key, returns new token (admin)
DELETE /auth/keys/{key_id}           # Revoke key permanently (admin)
### Scopes

| Scope | 可用操作 |
|---|---|
| `read` | GET 端點（agents, wallets, dashboard queries） |
| `write` | 授權付款、修改 agents、串流、Review Queue 決策 |
| `admin` | 金鑰管理、reset-demo-data |

---

## Solana USDC Payment Rail

```bash
GET  /solana/treasury                        # Treasury 資訊 + 餘額
POST /solana/settle/{transaction_id}         # 清算一筆已核准的交易
GET  /solana/payments                        # 列出鏈上清算紀錄
GET  /solana/payments?agent_name=ImageAgent  # 篩選特定 agent
```

**模擬模式（預設 `SOLANA_SIMULATE=true`）：**
在本地建立完整的 SPL Token `transfer_checked` 指令並以 Ed25519 treasury 金鑰簽名。
回傳 247 位元組的簽名交易作為憑證 — 證明金鑰擁有權與指令有效性，無需任何 RPC 呼叫。

**上線 devnet（`SOLANA_SIMULATE=false`）：**
向 `api.devnet.solana.com` 取得真實 blockhash 並廣播交易。
Treasury 金鑰存在 `.solana_treasury_keypair`（自動建立，`chmod 600`）。
用 devnet 水龍頭取得 USDC：https://spl-token-faucet.com/?token-name=USDC-Dev

---

## Simulator 模式

```bash
python agent_simulator.py normal
python agent_simulator.py loop
python agent_simulator.py prompt_injection
python agent_simulator.py large_payment
python agent_simulator.py anomaly
python agent_simulator.py streaming
python agent_simulator.py earn_and_invest
python agent_simulator.py identity
python agent_simulator.py tax_learning
python agent_simulator.py sdk
python agent_simulator.py auth           # NEW: 金鑰簽發 / 輪換 / scope 驗證
python agent_simulator.py solana         # NEW: 批准 → Solana devnet 清算
python agent_simulator.py credit
python agent_simulator.py tax
python agent_simulator.py mixed          # 完整 14 幕劇本
```

---

## Database schema

`alembic upgrade head` 建立以下資料表（含 indexes）：

```
agents            per-agent policies + status
transactions      all payment decisions
alerts            guard events + Slack webhook
wallets           per-agent USDC sub-wallets
streams           pay-as-you-go sessions
yield_positions   Morpho / Aave / RWA positions
identities        DID + signing key commitment
tax_labels        AI classifier training data
api_keys  NEW     key_id + sha256(secret) + scopes + expiry
solana_payments NEW  on-chain settlement records + tx_signature
```

---

## Production 差距說明（對投資人誠實）

| 項目 | 現狀（Demo） | Production 路線 |
|---|---|---|
| API key 雜湊 | SHA-256 | bcrypt / Argon2 |
| Agent signing key | secret 存 server 端 | agent 持有 Ed25519 私鑰，server 只存公鑰 |
| ZK owner proof | commit-and-reveal | 真實 ZK 系統（Semaphore / Groth16） |
| Solana USDC | devnet + 模擬 | mainnet + 真實 USDC + KMS 管理 treasury |
| Rate limiting | 無 | nginx / Kong / Cloudflare 前置 |
| Secrets | .env 檔案 | HashiCorp Vault / AWS Secrets Manager |
| Yield APY | 寫死常數 | 接真實 DeFi protocol API |

---

## Disclaimer

Demo only. No financial services, money transmission, custody, legal, tax, or investment advice.
Yield figures are illustrative. Solana transactions are on devnet (no real value).
