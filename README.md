# hark-bridge

> 作者：**君莫笑**
>
> **永恒之兰**
>
> MIT License

把 Hark 账号池暴露成标准 OpenAI 兼容接口。**不含注册环节**——会话由你自己在浏览器里登录后导入。

## 文档

| 文档 | 内容 |
|---|---|
| [docs/USAGE.md](docs/USAGE.md) | 从零到第一次调用，含故障排查 |
| [docs/PROTOCOL.md](docs/PROTOCOL.md) | 协议形态、状态机、变更适配指引 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 贡献规范与不接受的方向 |
| [CHANGELOG.md](CHANGELOG.md) | 版本记录 |

```
OpenAI 客户端 ──▶ hark-bridge  :8787/v1  ──▶ 账号池（cookie 会话）──▶ hark.com
```

## 这是什么

Hark 的聊天 API 不是 OpenAI 形态：

| 步骤 | Hark | OpenAI |
|---|---|---|
| 发送 | `POST /api/messages/send` → 立即返回 `messageId`，不含正文 | 单次调用返回答案 |
| 接收 | 轮询 `GET /api/messages/history` 取助手消息 | 内联返回或 SSE |
| 认证 | httpOnly Cookie | `Authorization: Bearer` |

本项目把这层差异隐藏起来，让下游只看到标准 OpenAI 接口。

## 边界（硬性）

本项目**只做协议适配与会话管理**，明确不含以下能力：

- **不提供注册功能**。没有任何自动建号、批量注册、邮箱验证代码。
- **不规避 Cloudflare**。不做 TLS 指纹伪装、不做 challenge 求解、不做验证码打码。
  Cloudflare 的人机验证需要真实浏览器，本项目不介入。
- **不共享账号**。设计用途是单个使用者管理自己的账号，不是对外发放访问权。

会话凭据必须由你本人用真实浏览器登录 Hark 后，经配套 Chrome 扩展导入。

## 安装

```bash
python -m pip install -r requirements.txt
```

需要 Python 3.11+。

## 配置

会话在磁盘上用 Fernet 加密。先生成密钥：

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

写进环境变量（缺了会直接抛错，不会退化成明文存储）：

```bash
# Windows PowerShell
$env:HARK_BRIDGE_KEY = "<上面生成的密钥>"
# Linux / macOS
export HARK_BRIDGE_KEY="<上面生成的密钥>"
```

可选环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `HARK_BRIDGE_KEY` | 必填 | Fernet 密钥，用于加密会话 |
| `HARK_BRIDGE_DB` | `./data/accounts.db` | 账号库路径 |
| `HARK_BRIDGE_PORT` | `8787` | 监听端口 |
| `HARK_BRIDGE_HOST` | `127.0.0.1` | 监听地址，默认只监听本地 |
| `HARK_BRIDGE_API_KEYS` | 空 | 客户端密钥，逗号分隔；空则不校验（仅限本地） |
| `HARK_BASE` | `https://hark.com` | 上游地址 |
| `HARK_REPLY_MODE` | `history` | 回复读取方式：`history` / `search` / `none` |
| `HARK_REPLY_TIMEOUT` | `120` | 等回复的超时秒数 |

端点路径可用环境变量覆盖（上游会变，不写死）：

```bash
HARK_PATH_SEND_MESSAGE=/api/messages/send
HARK_PATH_LIST_CONVERSATIONS=/api/conversations
```

## 导入会话

1. 在真实浏览器打开 `https://hark.com` 并登录
2. 装上 `extension/` 目录的扩展（见 [extension/README.md](extension/README.md)）
3. 点扩展图标 → 一键导入 → 扩展把会话推给本机服务，服务端复核后加密入库

授权台也可手动粘贴 Cookie：<http://127.0.0.1:8787/console>

## 运行

```bash
python -m hark_bridge
```

| 端点 | 说明 |
|---|---|
| `POST /v1/chat/completions` | OpenAI 兼容，支持 `stream: true` |
| `GET /v1/models` | 列出可用模型 |
| `GET /healthz` | 健康检查 |
| `GET /console` | 会话导入授权台（仅本地） |
| `GET /admin/accounts` | 账号池状态（脱敏） |

## 客户端接入

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <HARK_BRIDGE_API_KEYS 里配的密钥>" \
  -d '{"model":"hark-1","messages":[{"role":"user","content":"你好"}],"stream":true}'
```

## 账号状态

| 状态 | 含义 | 是否派单 |
|---|---|---|
| `session_valid` | 探针通过且会话未过期 | ✅ |
| `session_expiring` | 48 小时内到期，需要重新导入 | ✅ |
| `session_expired` | 已过期 | ❌ |
| `unverified` | 从未通过复核 | ❌ |
| `needs_interactive_verification` | 上游要求人机验证 | ❌ 终态 |
| `blocked` | 被风控 | ❌ 终态 |
| `credentials_rejected` | 凭据被拒 | ❌ 终态 |
| `rate_limited` | 限流中 | ❌ 退避重试 |

终态不会自动重试，只能重新导入会话后重置。

## 已知限制

- Hark 会**在服务端选模型**（Claude Opus / Sonnet / Haiku），没有模型选择器。
  因此 `model` 字段只做标识，不影响实际使用哪个模型。
- 回复正文靠轮询 `history` 获取，`isStreaming` 为 `true` 时表示仍在生成。
  本项目把累积文本切成小块推送，形成打字机效果，而非上游真流式。
- 这是逆向的私有 Web API，随时可能变更。端点路径可用环境变量覆盖以适应变化。
- 会话有有效期，过期后需重新在浏览器登录并导入。

## 许可

MIT © 2026 **君莫笑**

**永恒之兰**

## 免责声明

本项目与 Hark 无关，未获其授权或认可。它自动化访问第三方服务的私有 Web API，
使用者需自行确认并遵守该服务的服务条款及适用法律。仅供互操作与学习用途。
