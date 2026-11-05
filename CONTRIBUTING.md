# 贡献指南

> 作者：**君莫笑** · **永恒之兰**

## 欢迎的贡献

- 协议变更适配（上游端点、字段形态变化）
- 回复读取可靠性改进
- 文档修正与补充
- 测试覆盖补充

## 不接受的方向

以下改动不会被合并：

1. **任何形式的自动注册**——自动建号、批量注册、邮箱验证自动化
2. **Cloudflare / 风控规避**——TLS 指纹伪装、challenge 求解、验证码打码
3. **账号对外共享**——多租户分发、令牌转售

这不是技术品味问题。这些功能会让项目从"协议适配工具"变成"绕过服务方控制"，
既违反上游服务条款，也会让使用者账号更快被封。

## 开发环境

```bash
python -m pip install -r requirements.txt
python -m pip install pytest pytest-asyncio
```

## 跑测试

```bash
python -m pytest tests -q
```

测试全部使用 mock 上游，**不访问真实 hark.com**。新增测试也应如此。

## 代码约定

- 类型标注齐全，`from __future__ import annotations`
- 上游错误一律返回值（`CallResult`），不抛异常穿透
- 凭据永不出现在日志、返回值、异常消息里
- 状态变更必须经过 `state.py` 的闸门函数，不要在别处直接改状态
- 中文注释，解释"为什么"而非"是什么"

## 提交前检查

```bash
python -m pytest tests -q          # 全绿
python -c "import hark_bridge"     # 无导入错误
```

## 修改协议适配时

上游变了通常只需改这几处，不必动全局：

| 变了什么 | 改哪里 |
|---|---|
| 端点路径 | `endpoints.py` 默认值，或环境变量 |
| 建会话入参 | `upstream.py` 的 `_normalize_body()` |
| 回复匹配字段 | `reader.py` 的 `pick_reply()` |
| 会话 Cookie 名 | `session.py` 的 `DEFAULT_SESSION_COOKIE_PREFIXES` |

详细说明见 [docs/PROTOCOL.md](docs/PROTOCOL.md)。

## 提交信息格式

```
<类型>: <简短描述>

类型：feat / fix / docs / test / refactor / chore
```

例：

```
fix: history 轮询支持 v=2 与无版本降级
docs: 补充建会话嵌套入参说明
```

---

作者：**君莫笑**
**永恒之兰**
