# 使用说明

> 作者：**君莫笑** · **永恒之兰**

从零到第一次成功调用，照这个顺序做。

---

## 一、准备

### 1.1 环境要求

- Python 3.11 或更高
- 一个已经在浏览器里能正常登录的 Hark 账号

检查版本：

```bash
python --version
```

### 1.2 安装

```bash
git clone <本仓库地址> hark-bridge
cd hark-bridge
python -m pip install -r requirements.txt
```

验证依赖装好了：

```bash
python -c "import httpx, fastapi, cryptography; print('ok')"
```

### 1.3 生成加密密钥

会话在磁盘上必须加密存储。先生成一把密钥：

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

输出类似：

```
Qk3xYz9vL2mN8pR4sT6uW1yA5bC7dE0fG2hI4jK6lM8=
```

**把它保存好**。丢了就得重新导入所有账号——密钥换掉后，旧库里的会话解不开。

设置环境变量：

```powershell
# Windows PowerShell（当前窗口有效）
$env:HARK_BRIDGE_KEY = "Qk3xYz9vL2mN8pR4sT6uW1yA5bC7dE0fG2hI4jK6lM8="
```

```powershell
# Windows 永久生效（重新打开终端后仍有效）
[Environment]::SetEnvironmentVariable("HARK_BRIDGE_KEY", "你的密钥", "User")
```

```bash
# Linux / macOS
export HARK_BRIDGE_KEY="Qk3xYz9vL2mN8pR4sT6uW1yA5bC7dE0fG2hI4jK6lM8="
```

> 没设置这个变量就启动会直接报错。这是故意的——不允许退化成明文存储。

---

## 二、启动服务

先跑一次自检，确认环境齐备：

```bash
python -m hark_bridge.doctor
```

输出示例：

```
hark-bridge 0.1.0  ·  君莫笑  ·  永恒之兰
上游：https://hark.com

  ✓ 加密密钥 — 加解密往返正常
  ✓ 数据库读写 — 写入、加密、读回均正常
  ✗ 上游可达性 — 403 —— 可能被拦截
      提示：换网络环境重试；本项目不做指纹伪装
```

三项都在说什么：

| 检查项 | 它验证什么 |
|---|---|
| 加密密钥 | 密钥能加解密往返（不是只检查格式） |
| 数据库读写 | 用临时库实测写入 → 加密 → 读回，并断言明文没落盘 |
| 上游可达性 | 发一次**无 Cookie 的 GET**，看端点是否存在 |
| 端点路径 | 逐个试默认端点表，点名哪些返回了 404 |

**上游可达性报 403 是常见情况**——hark.com 对某些网络环境有拦截。
这时先解决网络问题，导入会话也大概率失败。这项检查不发送任何凭据。

自检通过后启动服务：

```bash
python -m hark_bridge
```

看到这样的输出就成功了：

```
INFO:     Uvicorn running on http://127.0.0.1:8787 (Press CTRL+C to quit)
```

默认只监听 `127.0.0.1`，外网访问不到。这是安全默认值，别改成 `0.0.0.0`。

检查服务活着：

```bash
curl http://127.0.0.1:8787/healthz
```

```json
{
  "status": "ok",
  "version": "0.1.0",
  "author": "君莫笑",
  "mark": "永恒之兰",
  "accounts_total": 0,
  "accounts_dispatchable": 0
}
```

---

## 三、导入会话

有两种方式，任选其一。

### 方式 A：Chrome 扩展（推荐）

**装扩展**

1. 打开 `chrome://extensions`
2. 右上角开启「开发者模式」
3. 点「加载已解压的扩展程序」，选择仓库里的 `extension/` 目录
4. 固定到工具栏

**导入**

1. 浏览器打开 <https://hark.com>，正常登录
2. 点扩展图标
3. 填账号标签（比如 `me-main`）
4. 点「读取 Cookie 并导入」

扩展会读取会话 Cookie，推给本机服务；服务端调 `get-session` 复核，
通过后加密入库。

### 方式 B：授权台页面

1. 浏览器打开 <https://hark.com> 并登录
2. 按 `F12` 打开开发者工具 → Network 标签
3. 刷新页面，随便点一个 `hark.com` 的请求
4. 在 Request Headers 里找到 `Cookie`，复制整行值
5. 打开 <http://127.0.0.1:8787/console>
6. 填账号标签、粘贴 Cookie，点「校验并入库」

三种粘贴格式都认：

```
# 格式 1：Cookie 头原文
__Secure-hark.session_token=abc123; theme=dark

# 格式 2：JSON
{"__Secure-hark.session_token": "abc123"}

# 格式 3：每行一个
__Secure-hark.session_token=abc123
theme=dark
```

### 校验结果怎么看

成功：

```json
{
  "ok": true,
  "account": "me-main",
  "state": "session_valid",
  "session_expires_at": 1793786400,
  "rotated": false
}
```

失败：

| 返回 | 含义 | 怎么办 |
|---|---|---|
| `"粘贴内容里没有会话 Cookie"` | 只粘了偏好 Cookie | 重新复制，确保含 `session_token` |
| `"探针返回空 session"` + `definitive: true` | 会话确实失效 | 重新登录后导入 |
| `"探针响应非 JSON 对象"` | 撞上拦截页 | 等一会儿重试，检查网络 |
| `"探针传输失败"` | 连不上上游 | 检查网络或代理 |

---

## 四、配置客户端密钥

默认不校验客户端密钥（方便本机直接用）。如果要加一层保护：

```powershell
$env:HARK_BRIDGE_API_KEYS = "sk-my-local-key-1,sk-my-local-key-2"
```

重启服务后，所有 `/v1/*` 请求都要带：

```http
Authorization: Bearer sk-my-local-key-1
```

`/console` 和 `/admin/*` 不受这个限制（它们只监听本地）。

---

## 五、调用

### 5.1 非流式

```bash
curl http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-my-local-key-1" \
  -d '{
    "model": "hark-1",
    "messages": [{"role": "user", "content": "简单介绍一下你自己"}]
  }'
```

### 5.2 流式

```bash
curl -N http://127.0.0.1:8787/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer sk-my-local-key-1" \
  -d '{
    "model": "hark-1",
    "messages": [{"role": "user", "content": "写一首四行诗"}],
    "stream": true
  }'
```

`-N` 关闭 curl 的缓冲，否则看不到流式效果。

### 5.3 列出模型

```bash
curl http://127.0.0.1:8787/v1/models \
  -H "Authorization: Bearer sk-my-local-key-1"
```

返回所有别名：`hark-1`、`gpt-4o`、`claude-opus-4` 等。
**它们都指向同一个上游**——Hark 没有模型选择器，服务端自己决定用哪个模型。

### 5.4 Python 客户端

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://127.0.0.1:8787/v1",
    api_key="sk-my-local-key-1",
)

response = client.chat.completions.create(
    model="hark-1",
    messages=[{"role": "user", "content": "你好"}],
)
print(response.choices[0].message.content)
```

流式：

```python
stream = client.chat.completions.create(
    model="hark-1",
    messages=[{"role": "user", "content": "讲个故事"}],
    stream=True,
)
for chunk in stream:
    delta = chunk.choices[0].delta.content
    if delta:
        print(delta, end="", flush=True)
```

### 5.5 接第三方客户端

任何支持自定义 OpenAI 兼容端点的工具都能用：

| 字段 | 值 |
|---|---|
| Base URL | `http://127.0.0.1:8787/v1` |
| API Key | `HARK_BRIDGE_API_KEYS` 里配的密钥（没配就随便填） |
| Model | `hark-1` |

常见工具：Cherry Studio、ChatBox、Open WebUI、LibreChat、Continue、Cline 等。

---

## 六、日常维护

### 6.1 看账号状态

```bash
curl http://127.0.0.1:8787/admin/accounts
```

返回脱敏信息，**不含任何凭据**：

```json
{
  "accounts": [
    {
      "account": "me-main",
      "label": "主号",
      "state": "session_valid",
      "session_expires_at": 1793786400,
      "last_probe_ok_at": 1793698000,
      "probe_attempts": 0
    }
  ]
}
```

### 6.2 手动复核某个账号

```bash
curl -X POST http://127.0.0.1:8787/admin/accounts/me-main/probe
```

### 6.3 删除账号

```bash
curl -X DELETE http://127.0.0.1:8787/admin/accounts/me-main
```

### 6.4 会话过期了怎么办

状态变成 `session_expired` 或 `session_expiring` 时：

1. 浏览器重新登录 hark.com
2. 用扩展或授权台重新导入
3. 状态自动恢复

**不要**指望它自己好——过期就是过期，只有重新登录能解。

---

## 七、故障排查

| 现象 | 原因 | 解决 |
|---|---|---|
| 启动报 `缺少 HARK_BRIDGE_KEY` | 没设密钥环境变量 | 按 1.3 设置 |
| 启动报 `不是合法的 Fernet 密钥` | 密钥复制不全 | 重新生成并完整复制 |
| `503 NoAccountAvailable` | 账号池为空或全部不可用 | 导入会话，或看 `/admin/accounts` |
| `502 all_accounts_failed` | 所有账号都失败 | 看响应里的 `note` 字段定位 |
| 回复为空但 `ok: true` | 轮询没取到正文 | 换 `HARK_REPLY_MODE=search` 试 |
| 流式没有逐字效果 | 上游本身是轮询非真流式 | 正常现象，见下方说明 |
| 端口被占用 | 8787 已被别的程序占 | 换 `HARK_BRIDGE_PORT` |

### 关于"流式"

Hark 上游没有真 SSE 推流（或推流路径未确认）。本项目的工作方式是：

1. 每 0.6~1.5 秒轮询一次历史
2. 拿到当前累积文本
3. 把**新增部分**切成 24 字符小块推给客户端

所以客户端看到的是打字机效果，但底层确实是轮询。
首字延迟取决于上游生成速度，通常 1~3 秒。

---

## 八、可选配置

| 变量 | 默认 | 说明 |
|---|---|---|
| `HARK_BRIDGE_API_KEYS` | 空 | 客户端密钥，逗号分隔；空则不校验 |
| `HARK_BRIDGE_DB` | `./data/accounts.db` | 账号库路径 |
| `HARK_BRIDGE_PORT` | `8787` | 端口 |
| `HARK_BRIDGE_HOST` | `127.0.0.1` | 监听地址 |
| `HARK_BASE` | `https://hark.com` | 上游地址 |
| `HARK_REPLY_MODE` | `history` | `history` / `search` / `none` |
| `HARK_REPLY_TIMEOUT` | `120` | 等回复超时（秒） |
| `HARK_BRIDGE_COOLDOWN` | `60` | 失败账号的冷却秒数 |

### 回复读取模式

| 模式 | 行为 | 适用 |
|---|---|---|
| `history` | 轮询历史，按 `triggeredByMessageId` 匹配 | 默认，最可靠 |
| `search` | 用搜索索引捞 | history 读不到时试 |
| `none` | 只投递不等回复，立即返回空 | 异步场景 |

---

## 九、安全提示

1. **别把 `HARK_BRIDGE_HOST` 改成 `0.0.0.0`**，除非你清楚风险。默认只听本地是有意的。
2. **密钥文件不要提交到 git**。`.gitignore` 已包含 `data/`、`*.db`、`.env`。
3. **`gateway_keys.json` 这类明文密钥文件不要放在仓库里**。本项目的客户端密钥只从环境变量读。
4. 多人共用一台机器时，注意 `HARK_BRIDGE_KEY` 的可见性——拿到它就能解密账号库。

---

作者：**君莫笑**
**永恒之兰**
