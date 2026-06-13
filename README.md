# AI Wallet Guard MVP v6

v6 升級為真正可部署的服務基礎設施：

| 功能 | v5 | v6 |
|---|---|---|
| 資料庫 | SQLite 本地檔案 | **PostgreSQL + SQLAlchemy 連線池 + Alembic migrations** |
| API 安全 | 無認證，任何人可呼叫 | **API 金鑰認證（Bearer token / X-API-Key，sha256 雜湊儲存，scope 管制）** |
| 支付清算 | 純模擬，無鏈上紀錄 | **Solana devnet USDC — 真實 SPL Token 交易指令 + Ed25519 簽名，模擬/上鍊皆可** |
| Schema 管理 | 手動建表 | Alembic migrations（`alembic upgrade head`） |
| 金鑰管理 | — | issue / rotate / revoke + scope 強制（read / write / admin） |
| Fallback | — | Postgres 不可達時自動降為 SQLite（zero-config CI/local dev） |

所有 v5 功能（DeID 身分、自學習稅務分類、SDK、串流支付、信用分、Review Queue、Yield Vault）完整保留。

---

## 新檔案

```text
db.py                  SQLAlchemy engine + 連線池 + SQLite fallback + param 正規化
auth.py                API key 簽發、驗證、輪換、scope 管制、bootstrap_root_key()
solana_rail.py         Solana devnet USDC 清算：SPL Token transfer_checked + Ed25519 簽名
migrations/
  env.py               Alembic 環境
  versions/
    0001_initial_schema_v6.py  完整 schema + indexes（含 api_keys, solana_payments）
alembic.ini            Alembic 設定
```

---

## 安裝

```bash
pip install -r requirements.txt
```

## 設定

建立 `.env`（或設環境變數）：

```bash
# 資料庫（必填，或設 DATABASE_BACKEND=sqlite 用 SQLite）
DATABASE_URL=postgresql+psycopg2://awg_user:awg_pass@localhost:5432/ai_wallet_guard_v6

# Solana（選填）
SOLANA_SIMULATE=true          # true = 本地簽名模擬；false = 真實 devnet 交易
SOLANA_RPC_URL=https://api.devnet.solana.com

# Slack（選填）
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/…   # critical alert 自動推播
```

## 初始化資料庫

```bash
# 建立 Postgres 使用者 + 資料庫（一次性）
createuser -P awg_user
createdb -O awg_user ai_wallet_guard_v6

# 跑 migration
alembic upgrade head
```

---

## 啟動

```bash
# Terminal 1 — Guard API（啟動時自動印出 root API key）
python -m uvicorn guard_api:app --reload --port 8000

# Terminal 2 — Dashboard
streamlit run app.py

# Terminal 3 — AI Agent Simulator
python agent_simulator.py mixed
```

---

## API 認證

所有 mutation 端點都需要帶金鑰：

```bash
# Bearer token
curl -H "Authorization: Bearer awg_xxx.yyy" http://localhost:8000/authorize-payment ...

# 或 X-API-Key header
curl -H "X-API-Key: awg_xxx.yyy" ...
```

Root key 在 API 第一次啟動時自動產生並印到 stdout，同時寫入 `.env`。

### 金鑰管理端點

```bash
POST   /auth/keys                    # 簽發新金鑰（admin scope）
GET    /auth/keys                    # 列出所有金鑰（admin）
POST   /auth/keys/{key_id}/rotate    # 輪換 → 廢棄舊金鑰，回傳新 token（admin）
DELETE /auth/keys/{key_id}           # 撤銷金鑰（admin）
```

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
