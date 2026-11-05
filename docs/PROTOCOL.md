# Hark 协议说明

> 作者：**君莫笑** · **永恒之兰**

本文记录 hark-bridge 所依赖的上游协议形态。目的是让后来者能理解代码为什么这样写，
以及在协议变更时该改哪里。

**本文只是形态记录，不包含任何自动化注册、风控规避手段。**

---

## 一、为什么需要一个桥

Hark 的聊天接口不是 OpenAI 形态：

| 步骤 | Hark | OpenAI |
|---|---|---|
| 发送 | `POST /api/messages/send` 立即返回 `{success:true, messageId}` | 单次调用返回答案 |
| 接收 | 轮询 `GET /api/messages/history` 取助手消息 | 内联返回或 SSE |
| 认证 | httpOnly Cookie（Better Auth） | `Authorization: Bearer` |
| 模型选择 | 服务端决定，无选择器 | 客户端指定 |

三个根本差异决定了桥的结构：

1. **没有"请求-响应"语义**——发送只表示"已投递"，正文要另外去找
2. **没有 bearer token**——只有 Cookie，所以必须有一条导入通道
3. **不能选模型**——客户端传的模型名只是别名

---

## 二、认证：Cookie 会话

### 2.1 Cookie 构成

Hark 用 [Better Auth](https://better-auth.com)，并把 `cookiePrefix` 改成了 `hark`：

| Cookie | 作用 | 是否凭据 |
|---|---|---|
| `__Secure-hark.session_token` | 会话主凭据 | ✅ |
| `__Secure-hark.session_token_multi-<id>` | multiSession 插件变体 | ✅ |
| `__Secure-hark.region` | 地区（`us` 等） | ❌ |
| `__Secure-hark.access` | 访问级别（`app`） | ❌ |
| `theme` | 界面偏好 | ❌ |

**这是最容易踩的坑**：判断"是否已登录"时看"hark.com 域下有没有 Cookie"会误判——
随便一个 `theme=dark` 都会被当成登录成功。一旦误判，账号以"可用"状态入库，
后面每个业务调用都失败，而且错误零散、无法归因。

### 2.2 唯一可靠的判定方式

```http
GET /api/auth/get-session
Cookie: __Secure-hark.session_token=...
Accept: application/json
```

返回：

```json
{
  "session": { "id": "...", "expiresAt": "2026-11-06T11:13:11.077Z" },
  "user":    { "id": "..." }
}
```

规则：

- `session.id` 存在 → 会话有效
- `session` 为空 / `null` → 会话确实不存在（`definitive = true`，可清凭据）
- 200 但返回 HTML → **不是失效**，是 Cloudflare 拦截页或网关页。
  属于"不确定"，凭据必须保留，否则一次误判就把好账号清掉。

代码位置：`hark_bridge/session.py` 的 `probe_session()`。

### 2.3 Token 滚动续期

Better Auth 在会话进入刷新窗口时，会随响应下发一枚新的 `session_token`，
旧 token 随即失效。

这意味着探针不能只做"校验"——它必须**把响应里的 `Set-Cookie` 收下并回写库**。
丢掉这一枚等于主动放弃续期，下一次带旧 token 就会被判未登录。

代码位置：`session.py` 的 `_response_session_cookies()` 与 `rotated` 字段。

---

## 三、端点

路径不写死在调用处，统一在 `hark_bridge/endpoints.py` 里，可用环境变量覆盖
（上游会变，这是必要的适配余地）。

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/auth/get-session` | GET | 会话唯一真源 |
| `/api/conversations` | GET | 列会话（Hark 侧"项目"就是 conversation） |
| `/api/conversations` | POST | 建会话，**入参必须嵌套** |
| `/api/messages/send` | POST | 发消息，只返回 `messageId` |
| `/api/messages/history` | GET | 取历史，`?conversationId=...&v=2` |
| `/api/search/messages` | GET | 搜索索引，部分账号不可用 |
| `/api/billing/summary` | GET | 额度，在 `meters.harkTokens` |

已确认**不存在**的路径（返回 404，别踩）：`/api/projects`、`/api/usage`、`/api/threads`。

### 3.1 建会话的嵌套入参

这是实测踩出来的：

```jsonc
// ❌ 400 invalid body
{ "autoTitle": "标题" }

// ❌ 报 autoTitle is required for projects
{ "title": "标题" }

// ✅ 正确
{ "project": { "autoTitle": "标题" } }
```

代码位置：`upstream.py` 的 `_normalize_body()`。

### 3.2 发消息

```http
POST /api/messages/send?cid=<conversationId>
Origin: https://hark.com
Referer: https://hark.com/chat
Content-Type: application/json

{ "conversationId": "...", "message": "你的问题" }
```

响应：`{ "success": true, "messageId": "..." }` —— **没有正文**。

---

## 四、回复正文的获取

这是整个协议里最需要解释的部分。

### 4.1 轮询历史

```http
GET /api/messages/history?conversationId=<cid>&v=2
```

返回 `messages` 字典（有时是数组，代码两种都兼容）：

```jsonc
{
  "messages": {
    "m1": { "role": "user",      "content": "问题", "timestamp": 1000 },
    "m2": {
      "role": "assistant",
      "content": "回答",
      "triggeredByMessageId": "<刚才的 messageId>",
      "timestamp": 2000,
      "isStreaming": false      // ← 关键字段
    }
  }
}
```

### 4.2 怎么确认"这是我这一轮的回复"

优先级：

1. **精确匹配**：`role == "assistant"` 且 `triggeredByMessageId == 刚发出的 messageId`
2. **时间戳兜底**：`timestamp / 1000 >= 发出时间 - 1` 的最新一条 assistant 消息
3. 都取不到 → 视为还没生成好，继续轮询

### 4.3 怎么知道"生成完了"

看 `isStreaming`：

- `true` → 仍在生成，继续轮询
- `false` 或字段缺失 → 已结束

代码位置：`reader.py` 的 `pick_reply()`。

### 4.4 轮询节奏

```
前 10 秒：每 0.6 秒一次   ← 首字延迟敏感
之后：   每 1.5 秒一次     ← 长回答不浪费请求
连续 30 轮无结果：放弃
```

先查后睡（而不是先睡后查），首字延迟从 2.5s+ 降到 1s 以内。

代码位置：`reader.py` 的 `poll_reply()`。

### 4.5 版本参数降级

`v=2` 不是所有部署形态都认。代码会在失败时交替尝试 `v=2` 与不带 `v`，
且只试有限的几次——不做无限重试把上游打爆。

---

## 五、请求头

浏览器会话需要的头：

```http
Origin: https://hark.com
Referer: https://hark.com/chat
```

缺了会被上游当成非页面来源拒绝。代码集中在 `upstream.py` 的 `BROWSER_HEADERS`。

---

## 六、账号状态机

```
                    ┌──────────────┐
      导入会话 ───▶ │  unverified  │
                    └──────┬───────┘
                           │ 探针通过
                           ▼
                    ┌──────────────┐   48h 内到期   ┌───────────────────┐
                    │ session_valid│ ─────────────▶ │ session_expiring  │
                    └──────┬───────┘                └───────────────────┘
                           │ 到期                            │
                           ▼                                 │
                    ┌──────────────┐ ◀───────────────────────┘
                    │session_expired│
                    └──────────────┘

    上游 401/403 ──▶ needs_interactive_verification （终态）
    被风控     ──▶ blocked                          （终态）
    凭据被拒    ──▶ credentials_rejected             （终态）
```

### 派单闸门

只有同时满足以下条件才允许向上游发请求：

1. 状态是 `session_valid` 或 `session_expiring`
2. 探针在 15 分钟内成功过
3. 会话未过期

任何一项不满足就拒绝，**不做善意推测**。

**为什么要这么严**：失效账号反复重试会持续放大上游对它的风控观测，
让一个本来只是过期的账号变成被拉黑的账号。宁可少用一个号，也不要打它。

代码位置：`state.py` 的 `is_dispatchable()`。

### 终态为什么不自愈

`needs_interactive_verification` / `blocked` / `credentials_rejected` 的账号，
自动重试 100 次也不会好——只有人重新在浏览器登录才能恢复。
所以它们被排除在派单池外，只能通过重新导入重置。

### 库状态之外还有一层：调度冷却

库里的状态回答"这个账号还能不能用"（会话层面，长期）。
调度冷却回答"这一刻该不该试它"（调度层面，短期，默认 60 秒）。

两者分开的理由：

- 一个账号可能只是临时 500，库状态仍是 `session_valid`，
  但这一分钟内不该再打它
- 冷却不该写进库，否则重启后还在，而那时的抖动早已过去
- 成功即解除冷却，避免因一次抖动长期不用某个号

冷却不改变库状态，`/healthz` 的 `accounts_benched` 单独反映它。

代码位置：`pool.py` 的 `_bench()` / `_is_benched()` / `benched_accounts()`。

---

## 七、凭据存储

- SQLite + Fernet 对称加密
- 密钥从环境变量 `HARK_BRIDGE_KEY` 读取
- **缺失直接抛错，不退化成明文存储**
- `/admin/accounts` 返回的脱敏快照不含任何凭据

代码位置：`store.py`。

---

## 八、协议变更时改哪里

| 上游变了 | 改哪 |
|---|---|
| 端点路径变了 | 环境变量 `HARK_PATH_*`，或 `endpoints.py` 默认值 |
| 建会话入参变了 | `upstream.py` 的 `_normalize_body()` |
| 回复匹配字段变了 | `reader.py` 的 `pick_reply()` |
| 会话 Cookie 名变了 | 环境变量或 `session.py` 的 `DEFAULT_SESSION_COOKIE_PREFIXES` |
| 新的 403 语义 | `upstream.py` 的 `call()` 状态落库逻辑 |

---

## 九、明确不做的事

1. **不做注册**。没有任何自动建号、批量注册、邮箱验证代码。
2. **不规避 Cloudflare**。不做 TLS 指纹伪装、不做 challenge 求解、不做验证码打码。
3. **不共享账号**。设计用途是单个使用者管理自己的账号。

理由：这三件事都会让工具从"协议适配"变成"绕过服务方控制"，
既违反上游条款，也会让账号更快被封。技术上能做不等于应该做。

---

作者：**君莫笑**
**永恒之兰**
