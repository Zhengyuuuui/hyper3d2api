# 3D 模型探测笔记 —— 补齐最后一块模型的缺口

> 对应约束见 `FEATURE.md`。日期：2026-10-08。分支：`feature`（不 push）。

---

## 1. 一句话结论

**3D 是独立 API 面，与视频/图像不是同一条路。**

| 项 | 结果 |
|--|--|
| 3D 端点 | `POST https://api.hyper3d.com/api/v2/rodin`（multipart/form-data） |
| 鉴权 | **`Authorization: Bearer <RODIN_API_KEY>`** —— 与网页登录 JWT **不通用** |
| 网页 JWT 调 v2 | ❌ `401 {"error":"UNAUTHORIZED","message":"Active API key is required."}`（5 个端点全部） |
| 视频/图像端点 | `POST /api/gateway/generate_from_{text,media}`（网页 JWT，走 `X-Hyper3D-Captcha`） |
| 结论 | 3D 模型 = 5 个 **tier**；要接入需**单独申请 API Key** |

---

## 2. 实测证据（Confirmed）

网页 session JWT 打 `/api/v2/*`：

```
404 GET  /api/v2/rodin         -> Cannot GET /api/v2/rodin      (路径存在, 方法不对)
401 POST /api/v2/rodin         -> {"error":"UNAUTHORIZED","message":"Active API key is required."}
401 POST /api/v2/status        -> 同上
401 POST /api/v2/download      -> 同上
401 POST /api/v2/bang          -> 同上
401 GET  /api/v2/check_balance -> 同上
```

→ **端点全部存在**，但需要另一套密钥。网页 JWT 在这里不被接受。

对比：同一 JWT 打 `/api/gateway/*` 完全正常（出片已验证），`/api/user/get_info` 也正常。
所以不是 token 失效，是 **v2 走独立的 API-KEY 体系**。

---

## 3. 3D 模型清单（tier）= 官方文档原文

来源：`docs.hyper3d.ai/en/api-specification/rodin-gen2-5`（Strong Evidence，官方文档）。

| tier（模型） | 用途 |
|--|--|
| `Gen-2.5-Extreme-Low` | 快速生成简单资产 |
| `Gen-2.5-Low` | 干净资产、小型硬表面道具 |
| `Gen-2.5-Medium` | 结构+细节均衡（默认档） |
| `Gen-2.5-High` | 更丰富结构、更平滑表面 |
| `Gen-2.5-Extreme-High` | 高频细节；**额外返回最高 10M 面的网格** |

> ⚠️ `tier` **必填**。不传会回落到 `Gen-1/1.5`（老模型）。

---

## 4. 请求规格

```
POST https://api.hyper3d.com/api/v2/rodin
Authorization: Bearer <RODIN_API_KEY>
Content-Type: multipart/form-data
```

| 字段 | 说明 |
|--|--|
| `images` | 1..5 张图（**图生3D 必填**；文生3D 省略） |
| `prompt` | ≤1024 字符（**文生3D 必填**；图生3D 可选，作朝向引导） |
| `tier` | **必填**，5 档之一 |
| `mesh_mode` | `Quad` / `Raw`（Gen-2.5 默认 `Raw`） |
| `geometry_file_format` | `glb`/`usdz`/`fbx`/`obj`/`stl`（默认 `glb`） |
| `texture_mode` | `legacy`/`extreme-low`/`low`/`medium`/`high`/`extreme-high` |
| `material` | `PBR`/`Shaded`/`Hybrid`/`All`/`None`（默认 `PBR`） |
| `quality` | `high`/`medium`/`low`/`extra-low` |
| `quality_override` | 500..2,000,000 面 |
| `seed` | 0..65535 |
| `bbox_condition` | 恰好 3 个整数 (x,y,z) |
| `addons` | 如 `HighPack`（2K→4K 贴图） |
| `quad_normal` / `soft` / `TAPose` / `use_original_alpha` / `preview_render` | bool |
| `detail_level` | 1..3 |
| `is_symmetric` | `symmetric`/`balanced`/`asymmetric`/`unknown` |
| `image_label` | 每张图的视角标签数组 |

**响应**（201）：
```json
{"message":"Submitted.","uuid":"<任务uuid>",
 "jobs":{"uuids":["<job uuid>"],"subscription_key":"<key>"},"consumed":0.5}
```
- `jobs.subscription_key` → 查状态（`POST /api/v2/status`）
- 顶层 `uuid` → 下载（`POST /api/v2/download`）

**计费**（加性）：基础 0.5；`Extreme-High` +0.5；`texture_mode=extreme-high` +2.0。

**Fast mode 触发条件**：`tier≠Extreme-High` && `geometry_file_format=glb` && `mesh_mode=Raw` && `quality_override≤20000`。

---

## 5. UI 侧佐证

Rodin 工作台（`/workspace/rodin`）实测可见：
- 模式：`Image to 3D` / `3D Editing` / `WorldGen`
- 档位下拉含：`Extreme-Low / Low / Medium / High / Extreme-High`（与文档一致）
- 生成按钮标 `1 Credit`
- 页面文案：`Gen-1.5 V1.2`、`Gen-2.5 Agent-Ready`

---

## 6. ⚠️ 本次探测的阻碍（重要）

**Turnstile 触发了 IP 级速率限制。**

- 症状：`turnstile.render()` 后 widget iframe **始终不加载**，回调永不触发（watchdog 超时）。
- 排除过程：
  1. 换标签页 → 仍失败
  2. **换全新 Chrome profile（`--user-data-dir` + 新端口 9391）→ 仍失败** ⇒ 不是 session/缓存问题，是 **IP 级**
  3. 同时业务 API（`/api/user/get_info`）**完全正常** ⇒ 只有 Turnstile 被限
- 成因：本轮探测累计 render 了 **20+ 次** Turnstile，触发 Cloudflare 对该出网 IP 的限流。
- 恢复：需**等待冷却**（通常几十分钟到数小时），或**更换出口 IP**。

> 📌 这是给 bridge 的重要工程结论：**Turnstile token 必须省着用**。
> 因此 `register_batch.py` 与 `bridge.py` 的「余额前置校验」不只是省钱，更是**保护 token 配额**。

---

## 7. 交付物

| 文件 | 内容 |
|--|--|
| `registry.py` `THREED` | 3D 端点/鉴权/参数/计费/tier 全量（官方文档） |
| `registry.py` `list_3d_models()` | 把 5 个 tier 作为 `family:"3d"` 模型暴露 |
| `/v1/models` | 现在返回 12 个模型（7 视频图像 + **5 个 3D tier**） |

---

## 8. 下一步（需用户决策）

| 选项 | 说明 |
|--|--|
| **A. 申请 `RODIN_API_KEY`** | 最干净：v2 无 Turnstile、无验证码，直接 3D。**推荐** |
| B. 抓网页 3D 请求 | 需等 Turnstile 冷却 + 驱动 Rodin 工作台抓真实请求，确认是否有 gateway 路径的 3D |
| C. 暂不接入 | 3D 需求不急，先兑现视频/图像 |

> 若选 A：把 key 放进 `config.json`（gitignored），我再补 `POST /v1/3d` 端点和 `rodin_client.py`。
> 在此之前 3D 标记为 **documented / needs API key**，不写死进 bridge 的可用模型。

---

*2026-10-08 · feature 分支 · 未 push*
