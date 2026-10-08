# hyper3d2api

OpenAI 兼容网关，封装 **Hyper3D (Rodin)** 的媒体生成接口。所有协议细节由 2026-10-08 的真实抓包 + 逐模型实测确定。

## 已验证可用的模型（Free 账号实测出片）

| 模型 | 类型 | 实测产物 |
|--|--|--|
| `pixverse` (`v6` / `v5.6` / `v5.5` / `v5` / `c1`) | 视频 | 5.04s MP4 / avc1+mp4a / 1,031,930B |
| `seedream_4_5` | 图像 | 2048×2048 JPEG / 907,384B |
| `seedream_5_0_lite` | 图像 | 2048×2048 JPEG / 314,467B |

**其余 12 个不可用**，原因已分类（`GET /v1/blocked-models`）：
- **provider 非法**：`sora-2` / `sora-2-pro` / `luma` / `luma-pro` —— 服务端 provider 枚举只有 12 个，不含 `sora2`/`luma`（前端 bundle 里的是占位值）。
- **服务端付费墙**：`veo3` / `minimax-h3-max` 等 → `NEED_ADVANCED_SUBSCRIPTION`。
- **无法伪造**：实测 `provider=pixverse model=sora-2`、`provider=veo model=v6` 等交叉组合全部被拒；provider↔model 绑定在服务端校验。

## 快速开始

```bash
cd ~/Documents/hyper3d2api
pip install -r requirements.txt

# 把 config.json 里的 token 换成你的 Hyper3D JWT（浏览器 localStorage.token）
python3 bridge.py --port 4870
```

**前置条件（运维模式 B，当前采用）**：常驻一个 headful Chrome（9366 端口），保持一个
`https://hyper3d.ai` 标签页开启。Turnstile token 必须在 `hyper3d.ai` 源下渲染（token 与 sitekey
域名绑定、**与登录态无关**），网关通过 CDP 9366 取 token，其余请求走纯 HTTP。

> **IP 限流事故归档（2026-10-08）**：同一出口 IP 短时间 render 20+ 次会触发 Cloudflare IP 级限流
> （widget iframe 不再加载、无 callback）。恢复 = 等冷却或换代理 IP。
> 预防规则：失败不立刻重试 render；连续 3 次失败熔断 ≥30min；能用余额/provider 前置校验拦住的请求绝不烧 token。
> 另有 `turnstile_ondemand.py`（方案 A：按需自启 headful Chrome，一次性 profile，用完自动 kill），
> 已验证生命周期可用，作为换 IP 后的即插即用通道保留。

## 接口

```bash
KEY="sk-hyper3d-local"

curl -H "Authorization: Bearer $KEY" http://127.0.0.1:4870/v1/models
curl -H "Authorization: Bearer $KEY" http://127.0.0.1:4870/v1/balance

# 视频（阻塞到出片，约 30-90s）
curl -X POST http://127.0.0.1:4870/v1/videos \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"v6","prompt":"a lighthouse at dusk","duration":5,"resolution":"720p","aspect_ratio":"16:9"}'

# 图像
curl -X POST http://127.0.0.1:4870/v1/images \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"seedream_4_5","prompt":"a red apple","size":"2048x2048"}'
```

响应：
```json
{"id":"job_…","status":"succeeded","upstream_uuid":"…","provider":"pixverse",
 "output_url":"https://file.hyper3d.com/gateway/…/output.mp4?<TOS签名>","balance_after":…}
```

Web 控制台：<http://127.0.0.1:4870/console>

控制台包含 6 个视图：
| 视图 | 内容 |
|--|--|
| 概览 | 号池/余额/用量/任务统计 + 上游连接状态 + 最近任务 |
| 号池 | 账号表（余额/套餐/状态），支持查余额、启停、删除、全部查余额 |
| 模型 | 可用模型（7 个）+ 不可用模型（13 个，含原因分类） |
| 生成 | 在线出片（选模型 → 填 prompt → 预览视频/图片） |
| 任务 | 任务列表，3s 自动刷新，带产物预览 |
| 台账 | 每次生成的扣费记录（来自 accounts.db 的 ledger 表） |

> 控制台复用网关自己的 `api_token` 鉴权；账户的原始 JWT **不会**下发到前端（列表只给 `token_preview`）。

## 上游协议要点（实测）

**生成端点**（注意必须带 `/gateway` 前缀）：
- `POST /api/gateway/generate_from_text` — JSON。**视频和图像都走这里**。
  `{"api":"text_to_video"|"text_to_image","provider":"pixverse"|"doubao","params":{...}}`
- `POST /api/gateway/generate_from_media` — FormData（`image_to_video` / `image_to_image` 等）
- `GET /api/gateway/result/<uuid>` — 轮询 `Queued → Running → Succeeded`

**必需请求头**：
```
Authorization: Bearer <localStorage.token>
X-Hyper3D-Captcha: turnstile:<一次性 token,752 字符,约5分钟过期,一次一用>
Ai-trace-id: trace-<Date.now()>-<random36[9]>
Content-Type: application/json      # FormData 模式不要设
```
- `model` 字段在 **`params` 内层**，不是顶层。
- pixverse 必带 `audio:true`。
- **Pro 档 = `api` 字符串加 `/pro` 后缀**（不是独立 model 值）。

**余额**：`POST /api/user/get_info`（body `{}`，结果在 `meta` 里；**GET 同路径 404**）。

## 错误分类（网关前置校验用，可省 Turnstile token）

| 错误 | HTTP | 含义 |
|--|--|--|
| `invalid_api` | 400 | provider 有效但该 api+model 组合不存在 |
| `param_invalid` | 400 | provider+api 有效但 model/参数值非法 |
| `paywall` | 402 | 服务端付费墙 |
| `insufficient_credits` | 402 | 余额不足 |
| `captcha_failed` | 502 | token 过期/重放 |
| `empty_response` | 502 | 上游返回 `201 {}` —— **静默失败，不扣费但也没任务，必须校验 uuid 非空** |

## 配置（config.json）

| 键 | 说明 |
|--|--|
| `api_token` | 网关自身的 key（客户端用这个 `Authorization`） |
| `token` | Hyper3D 上游 JWT |
| `cdp_port` | Chrome 调试端口（默认 9366） |
| `min_credits` | 余额低于此值直接拒绝，**不浪费 Turnstile token**（默认 5） |
| `wait_inline` | true = 阻塞到出片；false = 立刻返回 job id，用 `/v1/jobs/<id>` 轮询 |
| `human_pacing` | 随机延时 + 鼠标微动，降低被判定为脚本的概率 |

## 已知限制

- **计费不可预测**：上游标称 pixverse 0.5 credits，但实测单次扣 **5 或 10**。网关不硬编码价格，一律以 `get_info` 余额差为准。
- 余额不足时无法跑通（当前测试账号已耗尽，需充值后复验）。
- `image_to_video` / `image_to_image` 等 FormData 路径**尚未实测**（需要余额 + 上传文件）。