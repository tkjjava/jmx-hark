# hark-bridge Chrome 扩展

把浏览器里已登录的 Hark 会话导入本机 hark-bridge 服务。

**它只读 Cookie**，不注入页面、不修改上游状态、不自动登录或注册。

## 安装

1. 打开 `chrome://extensions`
2. 右上角开启「开发者模式」
3. 点「加载已解压的扩展程序」，选择本目录
4. 固定到工具栏

## 使用

1. 在浏览器打开 <https://hark.com> 并登录
2. 确认本机服务已启动：`python -m hark_bridge`
3. 点扩展图标 → 填账号标签 → 点「读取 Cookie 并导入」
4. 服务端会调 `get-session` 复核，通过才加密入库

## 它读取什么

只读取 `hark.com` 域下的会话 Cookie（`__Secure-hark.session_token` 及其分块/多会话变体）。
偏好 Cookie（如 `theme=dark`）会被忽略——它们不是凭据。

## 排查

| 现象 | 原因 |
|---|---|
| 「本机服务未启动」 | 先运行 `python -m hark_bridge` |
| 「没有找到会话 Cookie」 | 浏览器里还没登录 hark.com |
| 「粘贴内容里没有会话 Cookie」 | 会话已过期，重新登录 |
| 导入返回 `definitive: true` | 上游明确表示会话不存在，需要重新登录 |
