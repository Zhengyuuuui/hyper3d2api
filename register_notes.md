# Feature 分支笔记 —— 批量注册

> 对应约束见 `FEATURE.md`。本文件记录**过程、发现、坑**。分支**不 push**。

---

## 1. 结论（2026-10-08）

✅ **注册链路已跑通，批量可用。**

| 项 | 结果 |
|--|--|
| 端到端样本 | ✅ 成功（`xioca0lion8h` / uuid `07be329a…`） |
| 批量验证 | ✅ **2/2 成功**（`tmph3db4srklun`、`tmph3dbrk9jzlr`） |
| Turnstile | ✅ 复用 `turnstile.py`，1 次取 token 即过 |
| 邮箱验证码 | ✅ CF temp-email catch-all 收到，**13-27 秒** |
| 新号额度 | ✅ **balance = 50**，`plan = Free`，`trial_eligibility = true`（与原号同起点） |
| 单账号耗时 | 约 **13-27 秒**（send_code → 收码 → register） |

产物：**SQLite `accounts.db`**（唯一数据源，gitignored）+ `accounts.jsonl`（镜像导出）。

### 账号存储（SQLite）

`store.py` —— 账号库 + 用量台账。

```
accounts.db
├── accounts(id, email UNIQUE, username, password, user_uuid UNIQUE, token,
│            mailbox_id, balance, plan, trial_eligibility, status,
│            credits_used, gen_count, last_used_at, last_checked_at,
│            registered_at, note)
└── ledger(id, email, job_id, kind, model, credits, balance_after, created_at)
```

- `status`：`active` / `banned` / `exhausted` / `disabled`
- `pick_usable(min_balance)`：给 bridge 挑「可用且余额够」的号（余额降序 + 使用次数升序）
- `record_use(...)`：每次生成写台账（扣费 + 计数）
- `set_balance` / `mark_status`：状态回写
- `import_jsonl()`：幂等导入历史 `accounts.jsonl`（重复 email 走更新，**不覆盖运行时计数**）

```bash
python3 store.py init            # 建库
python3 store.py import-jsonl accounts.jsonl
python3 store.py list [--status active] [--min-balance 5]
python3 store.py stats           # 总数 / 总余额 / 生成数 / 已消耗
python3 store.py get <email>
python3 store.py verify          # 遍历全部账号，拉真实余额并回写
python3 store.py pick --min-balance 5
```

**入库结果（2026-10-08）**：4 个账号，合计 **200 credits**，全部 `active` / `Free`。

---

## 2. 邮箱服务（CF temp-email worker）

**Base**：`https://yunex-temp-email.zysgm123.workers.dev` · **Domain**：`yunex.ccwu.cc`

对应开源项目：`dreamhunter2333/cloudflare_temp_email`。

### 2.1 认证方式（关键，踩了很久）

| 身份 | 认证 |
|--|--|
| **Admin** | header **`x-admin-auth: <admin_passkey>`** |
| **地址用户** | header `Authorization: Bearer <address jwt>` |

> ⚠️ 坑：所有 `/api/*` 在未带正确凭证时统一返回纯文本 `Invalid address credential`（401），
> 极易误判成"接口不存在"。实际是**认证头不对**。正确答案来自开源仓库源码
> （`worker/src/utils.ts` → `checkIsAdmin` 读 `x-admin-auth`）。

### 2.2 用到的端点

```bash
BASE=https://yunex-temp-email.zysgm123.workers.dev
PW="$MAIL_ADMIN_PASSKEY"   # 从 config.json 读，勿硬编码

# 建临时地址（name 必填！domain 可选）
curl -X POST "$BASE/admin/new_address" \
  -H "x-admin-auth: $PW" -H 'Content-Type: application/json' \
  -d '{"name":"h3dtest","enablePrefix":true,"domain":"yunex.ccwu.cc"}'
# -> {"jwt":"eyJ...","address":"tmph3dtest@yunex.ccwu.cc","address_id":N}

# 读收件箱（用 address jwt）
curl "$BASE/api/mails?limit=20&offset=0" -H "Authorization: Bearer <jwt>"
# 读全文
curl "$BASE/api/mail/<id>" -H "Authorization: Bearer <jwt>"
# 管理员方式读任意邮箱
curl "$BASE/admin/mails?limit=20" -H "x-admin-auth: $PW"
curl "$BASE/admin/mails/<id>"     -H "x-admin-auth: $PW"
```

**字段约束**：`new_address` 的 **`name` 是必填**（漏了报 `Required field is missing`）；
带 `enablePrefix:true` 会生成 `tmp<name>@domain`；重名报 `Address already exists`。

---

## 3. 邮件解析坑（重要）

Hyper3D 的验证邮件正文：

```html
<p>Verification Code: <b>626651</b></p>
This code will expire in 15 minutes...
```

**⚠️ 不能用 `\b\d{6}\b` 直接抓** —— 邮件底部有一个 awstrack.me 追踪像素，
URL 里含 `.../000000/...`，会被优先匹配成 **`000000`**，导致注册失败
（`INVALID_EMAIL_VERIFICATION_CODE`）。

✅ 正确做法（`mailbox.py` 已实现）：
1. 优先匹配**带标签**的：`Verification\s*Code[^0-9]{0,40}(\d{6})`
2. 其次匹配**加粗**的：`<b>\s*(\d{6})\s*</b>`
3. 再兜底通用 `code…(\d{6})`
4. 且要**先 HTML→text 去标签**再匹配

另一个坑：`/api/mails` 列表返回的 `raw` **只有邮件头**，
必须再调 `/api/mail/<id>` 取全文才能拿到正文。

---

## 4. Hyper3D 注册链路

```
① POST /api/auth/send_email_verification_code
     {email, type:"Register"}   + X-Hyper3D-Captcha: turnstile:<token>
     -> 201 {}                     ← Turnstile 必需；此步不扣额度
② 收信取 6 位码（约 13-27s）
③ POST /api/auth/register
     {username, email, password, email_verification_code
      [, referral_code, marketing_email_consent]}
     -> {token, user_uuid}         ← 此步不要 captcha
④ token 直接当 `Authorization: Bearer <token>` 用
```

**字段约束**（服务端 400 原文）：`username ≤ 24`，`email ≤ 50`，`password ≥ 6`。

**验证**：新号 `POST /api/user/get_info` → `balance=50`，`plan=Free`。

---

## 5. 交付物

| 文件 | 说明 |
|--|--|
| `mailbox.py` | CF temp-email 适配（建址 / 读信 / **取码**，含防 `000000` 逻辑） |
| `register.py` | Hyper3D 注册客户端（`send_code` / `register` / `register_account`） |
| `register_batch.py` | 批量 runner（`--count` / `--dry-run`，串行 + 随机延时 + 落库 + 自动验证余额） |
| `accounts.jsonl` | 账号产物（**gitignored**） |

### 用法

```bash
python3 register_batch.py --count 1 --dry-run   # 只验证接线，不注册
python3 register_batch.py --count 1             # 注册 1 个（先跑样本！）
python3 register_batch.py --count 5             # 批量
```

`config.json` 需要的键：`mail_api_base` / `mail_admin_passkey` / `mail_domain` /
`cdp_port` / `site_key` / `register_delay` / `code_timeout`。

---

## 6. 待验证 / 风险

| 项 | 状态 |
|--|--|
| 单 IP 注册频控阈值 | ❓ 未知（本次 3 个账号零触发，未压测） |
| 邮箱域名是否被拉黑 | ❓ `yunex.ccwu.cc` 目前可用；未见黑名单 |
| `send_code` 冷却时间 | ❓ 未测 |
| 验证码有效期 | 邮件明写 **15 分钟**（Confirmed） |
| 并发注册 | ⚠️ 默认 **串行**（`register_concurrency:1`）；未验证并发安全性 |
| 新号能否真的生成 | ⚠️ 余额 50 已确认，但**未用新号实测生成**（建议抽 1 个号跑 `bridge.py` 验证） |
| 账号长期存活 | ❓ 未知（风控封禁策略未探） |

---

## 7. 下一步建议

1. 抽 **1 个新号**，用 `bridge.py` 实测一次生成（确认 50 额度真能出片）。
2. 若要规模化：先测**并发上限**与**单 IP 频率**，再决定是否加代理轮换。
3. 考虑给 `register_batch.py` 加：失败重试、断点续跑（读 `accounts.jsonl` 去重）、
   邮箱地址回收（`/admin/delete_address/:id`）。

---

*2026-10-08 · feature 分支 · 未 push*
