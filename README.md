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

**前置条件**：需要一个开着 `https://hyper3d.ai` 且已登录的 Chrome，**用 `--remote-debugging-port=9366` 启动**。
Turnstile token 必须在 `hyper3d.ai` 源下渲染（token 与 sitekey 域名绑定），所以网关通过 CDP 驱动那个 Chrome 取 token，其余请求走纯 HTTP。

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