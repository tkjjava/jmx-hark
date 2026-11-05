// 读取 hark.com 的会话 Cookie 并推送给本机 hark-bridge。
// 只读 Cookie，不注入页面、不修改任何上游状态。

const BRIDGE = 'http://127.0.0.1:8787';

// 只取会话 token。偏好 Cookie（theme 等）不是凭据，带上反而会被服务端拒。
const SESSION_PREFIXES = ['hark.session_token', 'better-auth.session_token'];

function isSessionCookie(name) {
  const bare = name.replace(/^__Secure-/, '').replace(/^__Host-/, '');
  return SESSION_PREFIXES.some((prefix) => bare.startsWith(prefix));
}

async function collectSessionCookies() {
  const all = await chrome.cookies.getAll({ domain: 'hark.com' });
  const picked = {};
  for (const cookie of all) {
    if (isSessionCookie(cookie.name)) {
      picked[cookie.name] = cookie.value;
    }
  }
  return picked;
}

async function probeBridge() {
  try {
    const response = await fetch(`${BRIDGE}/healthz`);
    return response.ok ? await response.json() : null;
  } catch {
    return null;
  }
}

async function importToBridge(account, label) {
  const cookies = await collectSessionCookies();
  const names = Object.keys(cookies);
  if (names.length === 0) {
    return {
      ok: false,
      note: '没有找到会话 Cookie。请先在 hark.com 页面登录。'
    };
  }

  // 转成 Cookie 头形态交给服务端，避免扩展自己拼 JSON 出错
  const cookieHeader = names.map((n) => `${n}=${cookies[n]}`).join('; ');

  try {
    const response = await fetch(`${BRIDGE}/admin/import`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ account, label, cookie: cookieHeader })
    });
    const payload = await response.json();
    return { ...payload, found: names };
  } catch (error) {
    return {
      ok: false,
      note: `无法连接本机服务 ${BRIDGE}：${error}`
    };
  }
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message.type === 'probe') {
    probeBridge().then(sendResponse);
    return true;
  }
  if (message.type === 'import') {
    importToBridge(message.account, message.label).then(sendResponse);
    return true;
  }
  return false;
});
