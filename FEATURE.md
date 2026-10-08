# feature 分支工作约束（批量注册）

> 本文件是 `feature` 分支的**强制约束**。任何在本分支动手的人（含 AI agent）开始前必须先读本文件。
> 建立时间：2026-10-08 · 分支：`feature` · 基线：`main`（fc05e3c）

---

## 0. 一句话

`feature` 分支**只负责批量注册**（Hyper3D 账号批量创建）。它**永远不 push 到 remote**，
成果只在本地保留；确认可用后再由人工决定是否以别的形式合回 `main`。

---

## 1. 分支纪律（硬规则）

| 规则 | 说明 |
|--|--|
| **❌ 禁止 push** | **`git push` 一律不允许**，无论 `origin feature` 还是任何 remote。本分支不设 upstream，这是刻意的。 |
| **❌ 禁止 merge 到 main** | 未经人工明确指令，不得 merge / rebase / cherry-pick 到 `main`。 |
| **❌ 禁止 force-push** | 任何形式。 |
| **✅ 允许本地 commit** | 本地提交是鼓励的（便于回溯），但不推远端。 |
| **✅ 允许改本分支文件** | 见 §3 文件归属。 |
| **✅ 只读 main 的既有模块** | 可 import/复用，但不要顺手改动 `main` 的实现（需要改 → 先说明为什么）。 |

> 判断口诀：**"这个命令会不会让远端发生变化？"** 会 → 停。
> `git push` / `gh repo` 写操作 / `git remote` 修改 —— 全部禁止。

### 自检命令（每次会话结束前跑）
```bash
cd ~/Documents/hyper3d2api
git rev-parse --abbrev-ref HEAD            # 必须是 feature
git rev-parse --abbrev-ref @{u} 2>&1       # 必须报 "no upstream"（正确）
git status --short                         # 有无遗留在暂存区
```

---

## 2. 本分支的任务范围

**目标**：把 Hyper3D 的**批量注册**做成可运行的模块。

**已探明的注册链路**（来自 `~/Documents/search example/sessions/2026-10-08-hyper3d-ai/` §8）：
```
1. POST /api/auth/send_email_verification_code   {email, type:"Register"}   + X-Hyper3D-Captcha  -> 201
2. 收邮件取 6 位验证码
3. POST /api/auth/register  {username,email,password,email_verification_code
                             [,referral_code, marketing_email_consent]}                        -> {token,user_uuid}
4. token 落 localStorage.token -> 即可调 /api/gateway/*
```

**字段约束（服务端 400 原文，Confirmed）**：
- `username` ≤ 24 字符
- `email` ≤ 50 字符，必须是合法 email
- `password` ≥ 6 字符
- `email_verification_code` 必填，`000000` 会被拒（`INVALID_EMAIL_VERIFICATION_CODE`）
- `referral_code` / `marketing_email_consent` 可选
- `send_email_verification_code` 的 `type ∈ {Register, ChangeEmail, ResetPassword}`
- **`register` 本身不要 Turnstile；只有 `send_code` 要**

**Turnstile**：复用现成的 `turnstile.py`（同一 sitekey `0x4AAAAAADTv8yKBtK5-Zt1E`），方法见
`~/Documents/fk/cfTurnstile.md`。

---

## 3. 文件归属（不要越界）

### ✅ 本分支新增（自由编辑）
| 文件 | 用途 |
|--|--|
| `register.py` | 批量注册主逻辑（`send_code → 收码 → register → 存 token`） |
| `mailbox.py` | 收信抽象：catch-all 域名 / 临时邮箱 API 适配层 |
| `accounts.jsonl` | **账号产物**（每行一个 `{email,password,user_uuid,token,created_at}`）—— **绝不可提交** |
| `register_notes.md` | 过程笔记 |
| `FEATURE.md` | 本文件 |

### 🔒 `main` 的既有模块（只读复用，改需说明）
`bridge.py` · `hyper3d.py` · `registry.py` · `turnstile.py` · `console.html` · `README.md` · `config.example.json` · `requirements.txt`

> 需要改这些文件时：先在回复里**明确说明"改哪个文件、为什么、影响什么"**，再动手。

### 🚫 永远不进版本库（见 `.gitignore`）
- `config.json`（含真实 JWT）
- `accounts.jsonl`（含批量账号凭证）
- 任何 `*.token` / `mailbox.json`（含邮箱 API key）
- `__pycache__/`、`*.log`

---

## 4. 安全红线

1. **凭证隔离**：真实 token / 邮箱密码 / 邮箱 API key **只进本地配置或 .gitignore 文件**，永不硬编码进 `.py`。
2. **`accounts.jsonl` 视同密钥库**：一旦 push 泄露，等于泄露全部账号。（本分支不 push，天然规避；但仍要保持敏感。）
3. **提交前扫描**（每次 commit 前跑）：
   ```bash
   git diff --cached | grep -nE "eyJhbGciOi|Bearer [A-Za-z0-9._-]{20,}|sk-[A-Za-z0-9]{16,}|gho_|AKLT" || echo "clean"
   ```
4. **临时邮箱 API key** 走 `config.json`（gitignored），不写进代码。

---

## 5. 工程约定

- **一次一账号**：先做通 **1 个端到端样本**，再谈并发/批量（不要一上来跑 100 个）。
- **节奏控制**：注册间随机延时（参考 `turnstile.py` 的 `human_pacing` 思路），避免触发风控。
- **失败可追溯**：每个账号的每个阶段（send_code / 收码 / register）都要可记录、可重试、可跳过。
- **不硬编码**：邮箱来源、并发数、延时全部走 `config.json`。
- **复用优先**：Turnstile 用 `turnstile.py`，HTTP 客户端风格对齐 `hyper3d.py`（httpx + 错误分类）。

---

## 6. 下一个 chat 的启动清单

按顺序执行：

1. `cd ~/Documents/hyper3d2api && git rev-parse --abbrev-ref HEAD`
   → **必须是 `feature`**；若在 `main`，先 `git checkout feature`。
2. 读本文件（`FEATURE.md`）+ `~/Documents/fk/cfTurnstile.md`。
3. 确认邮箱方案（**需用户决定**，见下方待决项）。
4. 写 `mailbox.py`（收信适配）→ 写 `register.py`（注册主流程）。
5. **先跑 1 个样本**，记录：新号余额、耗时、是否触发风控。
6. 只有样本通过后才考虑并发，**且始终不 push**。

### ⚠️ 启动前必须向用户确认的待决项

| # | 待决问题 | 影响 |
|--|--|--|
| A | **邮箱方案**：自建 catch-all 域名（MX 收信）/ 第三方临时邮箱 API / 已有邮箱池？ | 决定 `mailbox.py` 怎么写 |
| B | 若用临时邮箱 API：是哪个服务、有无 API key、限速如何？ | 决定适配层实现 |
| C | 目标账号数量 + 并发数 + 是否轮换代理？ | 决定风控策略 |
| D | 注册成功后 token 的用途（直接供 `bridge.py` 池化使用？） | 决定 `accounts.jsonl` 结构 |

> 用户未回答 A 之前，不要猜着写收信代码。

---

## 7. 已知未验证项（写代码时留出处理）

- ❓ 新账号注册后是否赠送 credits、送多少（当前主号 50 起，未验证新号）
- ❓ 单 IP 注册频控阈值
- ❓ 一次性邮箱域名是否被黑名单（`example.com` 的 send_code 请求被接受，但未测能否注册成功）
- ❓ `send_email_verification_code` 的**冷却时间**与**有效期**（未测）
- ❓ 是否需要 `marketing_email_consent` 才放行（当前判定为可选）

---

## 8. 红线速查（贴在心里）

```
不 push      不 merge     不 force-push
不提交 config.json / accounts.jsonl / 任何凭证
改动 main 既有文件前先说明
先 1 个样本，再批量
```

---

*本约束由 2026-10-08 会话建立。修改本文件视为修改约束本身，需明确说明理由。*
