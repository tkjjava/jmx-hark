# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

---

## [0.1.0] - 2026-11-05

首个版本。

### 新增

- **自检工具** `python -m hark_bridge.doctor`
  - 加密密钥：实测加解密往返，不只检查格式
  - 数据库：临时库实测写入 → 加密 → 读回，断言明文不落盘
  - 上游可达性：无 Cookie 的 GET，不发任何凭据
  - 端点路径：逐个试默认端点表，点名 404 的那几个
- **OpenAI 兼容接口**
  - `POST /v1/chat/completions`（非流式 + SSE 流式）
  - `GET /v1/models`（模型别名表）
- **会话管理**
  - `probe_session()`：以 `get-session` 为唯一判定依据
  - Token 滚动续期：收下响应 `Set-Cookie` 并回写库
  - `definitive` 判定：区分"会话失效"与"拦截页返回 HTML"
- **账号池**
  - SQLite + Fernet 加密存储，密钥缺失直接抛错
  - 8 状态状态机，3 个终态不自动重试
  - 派单闸门：状态 / 探针新鲜度 / 会话有效期三重校验
  - 轮转调度 + 失败自动换号
- **回复读取**
  - 轮询历史，`triggeredByMessageId` 精确匹配 + 时间戳兜底
  - `isStreaming` 判定生成是否结束
  - 自适应节奏：前 10 秒 0.6s，之后 1.5s
  - `v=2` 与无版本降级
  - 三种模式：`history` / `search` / `none`
- **会话导入**
  - Chrome 扩展一键导入（只读 Cookie）
  - 本地授权台 `/console`，支持三种粘贴格式
  - 偏好 Cookie 一律拒绝
- **文档**
  - `docs/USAGE.md`：从零到第一次调用
  - `docs/PROTOCOL.md`：协议形态与变更适配指引

### 测试

43 项，全部使用 mock 上游，不访问真实站点。覆盖：

- Cookie 识别（含分块 / multiSession 变体、偏好 Cookie 排除）
- 状态机（终态不派单、探针过期、会话过期、expiring 仍可派单）
- 回复挑选（精确匹配 / 时间戳兜底 / streaming 未完成 / 忽略空内容）
- 端到端派单链路（建会话 → 发消息 → 轮询取回复）
- 上游 403 落终态
- 凭据加密落盘（断言明文不出现在库文件里）
- 拦截页 HTML 不清凭据

### 明确不含

- 自动注册 / 批量建号
- Cloudflare / 风控规避
- 账号对外共享

---

作者：**君莫笑**
**永恒之兰**
